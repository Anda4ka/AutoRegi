"""NFT Trencher board feed → the mint calendar, and T-1h reminders.

The board at neverfuckingtrade.com publishes, as a documented JSON API, the
same slice its page shows: mints a week ahead across Robinhood Chain,
Ethereum, Ink and HyperEVM, with every stage's time, price and per-wallet
limit, read from OpenSea and the chain (public stages on Ink/HyperEVM are
read on-chain every round). That is a supplier, not a competitor — and its
data has provenance, unlike an LLM's guess, which is why it goes straight
into the calendar instead of through the quote gate.

Two rules keep it honest anyway:

* **The feed is a mirror, not a merge.** Rows are written under the feed's
  own project ids and only *attached* to a bot project when the registry link
  matches — an import never rewrites what the operator wrote by hand, and a
  mismatch costs nothing but a `(NoReg)` row.
* **Reminders are opt-in per user** (`users.mint_remind`, off by default),
  delivered through the broadcast outbox — the same pacing, 429/403 handling
  and idempotency as every other mass send, never a send_message loop.

Everything here is dark until both `MINTS_ENABLED=1` and `NFTT_API_KEY` are
set; the key requires holding the board's Cassette NFT and dies on its own
when the Cassette leaves the wallet (their docs: within ten minutes).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from bot import config
from bot import db as bot_db
from bot import mints as bot_mints

TIMEOUT = (10, 30)
# The board rebuilds every ~40 s and its answers cache for 15 s; the alert
# loop sweeps every ADMIN_ALERT_INTERVAL (default 300 s) and a marker keeps
# the sync to this cadence regardless.
SYNC_EVERY_SEC = 300
# Chain names as our calendar shows them (the calendar column is free text).
CHAIN_LABEL = {
    "robinhood": "Robinhood Chain",
    "ethereum": "Ethereum",
    "ink": "Ink",
    "hyperevm": "HyperEVM",
}
# Their stage kinds → our phase keys; normalize_phase keeps unknowns anyway.
KIND_MAP = {
    "public": "public",
    "gtd": "gtd",
    "fcfs": "fcfs",
    "wl": "wl",
    "og": "og",
    "holder": "holder",
    "raffle": "raffle",
    "team": "team",
    "other": "other",
}
FLAG_RU = {
    "price_switch": "цена сменилась посреди окна",
    "locked": "контракт залочен",
}


def _log(msg: str) -> None:
    print(f"[nftt] {msg}", flush=True)


def feed_enabled() -> bool:
    return bool(config.MINTS_ENABLED and getattr(config, "NFTT_API_KEY", ""))


def fetch_mints() -> list[dict] | None:
    """The whole board slice, or None on any failure (a dead feed must not
    look like an empty one — that distinction is the whole lesson of the
    mint scanner's ExtractionDown)."""
    if not feed_enabled():
        return None
    try:
        r = requests.get(
            f"{config.NFTT_BASE_URL}/mints",
            headers={"X-API-Key": config.NFTT_API_KEY},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        _log(f"sync failed: {type(exc).__name__}")
        return None
    if r.status_code == 401:
        # Key dead: Cassette sold or revoked. Loud and once per sync, not
        # per row — the calendar keeps what it already has.
        _log("401 — ключ не работает (Cassette продан/отозван?)")
        return None
    if r.status_code != 200:
        _log(f"sync failed: HTTP {r.status_code}")
        return None
    try:
        data = r.json()
    except ValueError:
        _log("sync failed: ответ не JSON")
        return None
    mints = data.get("mints")
    return mints if isinstance(mints, list) else None


# --- registry matching -----------------------------------------------------


def _norm(url: str) -> str:
    """Host + path without www/trailing slash — enough to call two links the
    same project without being fooled by scheme or tracking crumbs."""
    raw = str(url or "").strip().lower()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    p = urlparse(raw)
    return (p.netloc.removeprefix("www.") + p.path.rstrip("/")).rstrip("/")


def _registry_link_index() -> dict[str, str]:
    from bot.registry import list_projects

    index: dict[str, str] = {}
    for p in list_projects():
        for link in (getattr(p, "twitter_url", ""), getattr(p, "link", "")):
            key = _norm(link)
            if key:
                index[key] = p.id
    return index


def _match_project(mint: dict, index: dict[str, str]) -> str:
    links = mint.get("links") or {}
    for key in (_norm(links.get("x")), _norm(links.get("site"))):
        if key and key in index:
            return index[key]
    return ""


def _pid_for(mint: dict) -> str:
    slug = re.sub(r"[^a-z0-9_-]", "", str(mint.get("slug") or "").lower())
    return f"nftt_{slug or mint.get('id', '')[:32]}"


# --- sync -------------------------------------------------------------------


def _stage_price(stage: dict) -> str:
    price = stage.get("price")
    if price is None:
        return ""
    cur = str(stage.get("currency") or "").strip()
    return f"{price} {cur}".strip()


def sync_to_calendar(mints_list: list[dict] | None = None) -> int:
    """Upsert the board into project_mints/mint_phases. Returns rows written."""
    if mints_list is None:
        mints_list = fetch_mints()
    if mints_list is None:
        return -1
    index = _registry_link_index()
    written = 0
    for m in mints_list:
        status = str(m.get("status") or "")
        if status == "ended":
            continue
        matched = _match_project(m, index)
        pid = matched or _pid_for(m)
        name = str(m.get("name") or "")[:60] or pid
        if not matched:
            # Operator rule 2026-08-20: a project the bot never registered for
            # answers «can I still get on this list from here?» with (NoReg).
            name = f"(NoReg) {name}"
        links = m.get("links") or {}
        flags = [FLAG_RU.get(f, f) for f in (m.get("flags") or [])]
        tier = str(m.get("tier") or "")
        note_bits = [b for b in (f"тир: {tier}" if tier else "", *flags) if b]
        supply = m.get("supply")
        bot_mints.set_mint(
            pid,
            supply=int(supply) if isinstance(supply, int) else None,
            chain=CHAIN_LABEL.get(str(m.get("chain") or ""), str(m.get("chain") or "")),
            note=" · ".join(note_bits),
            source_url=str(links.get("site") or links.get("x") or ""),
            mint_url=str(links.get("mint") or links.get("opensea") or ""),
            name=name,
            source="nftt",
        )
        for stage in (m.get("stages") or []):
            # Team/dev/reserve windows nobody outside the project can enter
            # are noise on a reader's calendar.
            if stage.get("open_to_visitors") is False:
                continue
            starts = bot_mints.parse_when(stage.get("start"))
            ends = bot_mints.parse_when(stage.get("end"))
            note_parts = []
            mpw = stage.get("max_per_wallet")
            if isinstance(mpw, int) and mpw > 0:
                note_parts.append(f"лимит {mpw}/кошелёк")
            if ends:
                note_parts.append(f"до {ends[:16].replace('T', ' ')} UTC")
            bot_mints.set_phase(
                pid,
                KIND_MAP.get(str(stage.get("kind") or ""), str(stage.get("kind") or "other")),
                starts_at=starts,
                price=_stage_price(stage),
                note=" · ".join(note_parts),
                label=str(stage.get("label") or "")[:32],
            )
        written += 1
    if written:
        bot_db.set_setting("nftt_last_sync", datetime.now(timezone.utc).isoformat())
    _log(f"synced {written} mints")
    return written


def maybe_sync() -> int:
    """One sync per SYNC_EVERY_SEC from the alerts loop; off = -1/0."""
    if not feed_enabled():
        return -1
    last = bot_db.get_setting("nftt_last_sync", "")
    if last:
        try:
            if (datetime.now(timezone.utc)
                    - datetime.fromisoformat(last)).total_seconds() < SYNC_EVERY_SEC:
                return 0
        except ValueError:
            pass
    return sync_to_calendar()


# --- T-1h reminders -----------------------------------------------------------


def _reminder_window(now: datetime) -> tuple[datetime, datetime]:
    lead = max(5, int(config.MINT_REMIND_LEAD_MIN))
    return now + timedelta(minutes=lead - 10), now + timedelta(minutes=lead + 10)


def _reminder_text(entry: dict, phase: dict) -> str:
    when = str(phase.get("starts_at") or "")[:16].replace("T", " ")
    price = f" · {phase['price']}" if phase.get("price") else ""
    note = f" · {phase.get('note')}" if phase.get("note") else ""
    url = entry.get("mint_url") or entry.get("source_url") or ""
    body = (
        f"⏰ Минт через ~{int(config.MINT_REMIND_LEAD_MIN)} мин\n"
        f"{entry['project']} · {phase['label']} · {when} UTC{price}{note}"
    )
    if url:
        body += f"\n{url}"
    return body


def reminders_due(now: datetime | None = None) -> list[tuple[dict, dict]]:
    """Calendar phases whose start falls inside the T-1h window right now."""
    now = now or datetime.now(timezone.utc)
    lo, hi = _reminder_window(now)
    out = []
    for entry in bot_mints.calendar():
        for phase in entry.get("phases") or []:
            raw = phase.get("starts_at")
            if not raw or phase.get("past"):
                continue
            try:
                start = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if lo <= start <= hi:
                out.append((entry, phase))
    return out


def fire_reminders(now: datetime | None = None) -> int:
    """One batch per due phase, through the broadcast outbox. Returns batches."""
    from bot import broadcast

    if not config.MINTS_ENABLED:
        return -1
    audience = bot_db.mint_remind_user_ids()
    if not audience:
        return 0
    sent = 0
    for entry, phase in reminders_due(now):
        # Marker per phase start: a phase moved → new time → new reminder;
        # the same phase keeps its marker, so a restart cannot re-blast it.
        marker = "mintrem:{pid}:{kind}:{at}".format(
            pid=entry["project_id"], kind=phase["kind"], at=phase["starts_at"])
        if bot_db.get_setting(marker, ""):
            continue
        batch = "mintrem-" + re.sub(r"[^a-z0-9-]", "", marker.lower())
        broadcast.enqueue(
            batch,
            audience,
            body=_reminder_text(entry, phase),
            interval_sec=0.05,
        )
        bot_db.set_setting(marker, "1")
        sent += 1
        _log(f"reminder queued: {entry['project']} {phase['kind']} "
             f"for {len(audience)} users")
    return sent


def sweep() -> None:
    """Both halves, as the alerts loop calls them. Never raises."""
    try:
        maybe_sync()
    except Exception as exc:  # noqa: BLE001 — the loop must survive the feed
        _log(f"sync error: {exc}")
    try:
        fire_reminders()
    except Exception as exc:  # noqa: BLE001
        _log(f"reminders error: {exc}")
