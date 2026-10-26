"""The mint calendar: what is known about each project's drop, and who said so.

Three rules shape everything here, and each of them came from a measurement
rather than a preference.

**Nothing unconfirmed reaches a reader.** The calendar reads `mint_phases`;
proposals live in `mint_proposals` and only an accepted one writes across. This
is not ceremony: asked for mint details, both LLM endpoints tested on
2026-08-17 produced confident answers with fabricated source URLs — one of them
gave the same wrong date twice, which reads as corroboration. A human gate is
the only thing that stops that reaching people who would act on it.

**Times are UTC in the database and local on the screen.** A calendar whose
reader has to convert in their head is a calendar people miss mints by.

**Undated is a state, not a gap.** `starts_at IS NULL` means the project has
announced a mint without a date — which is where most of them sit. It has to
be distinguishable from "no mint known", or the screen either hides real
projects or invents certainty it does not have.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from bot import db as bot_db

# The phases a drop actually has. Order is the order they happen in, which is
# also the order they should be listed in; `tba` sorts last because it is the
# absence of a time rather than a late one.
KNOWN_PHASES = ("gtd", "fcfs", "wl", "public", "tba")
PHASES = KNOWN_PHASES          # kept for callers that list the common ones
PHASE_RU = {
    "gtd": "GTD",
    "fcfs": "FCFS",
    "wl": "WL",
    "public": "Public",
    "tba": "TBA",
}
# Anything else a project names its window — Ratlist, Team, Holder Claim — is
# kept as written. Collapsing them into "tba" said "no window given" about a
# window that had a name, a time and a price.
_SLUG = re.compile(r"[^a-z0-9а-яё_-]+")
MAX_PHASE = 24


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_phase(kind: str) -> str:
    """A stable key for a phase name, keeping unknown ones instead of erasing them."""
    k = (kind or "").strip().lower()
    if k in KNOWN_PHASES:
        return k
    k = _SLUG.sub("_", k).strip("_")[:MAX_PHASE]
    return k or "tba"


def phase_label(kind: str, label: str = "") -> str:
    """What to print for a phase: the project's own wording when it gave one."""
    if label:
        return label
    if kind in PHASE_RU:
        return PHASE_RU[kind]
    return kind.replace("_", " ").upper()


def parse_when(raw: str | None) -> str | None:
    """Accept what a human types and store UTC ISO, or None for TBA.

    Deliberately narrow: an ambiguous date is worse than no date, because the
    calendar will show it as fact. Anything not clearly parseable is refused
    rather than guessed at.
    """
    text = (raw or "").strip()
    if not text or text.lower() in ("tba", "тба", "-", "—", "null", "none"):
        return None
    # DD.MM.YYYY is what a Russian keyboard produces and it is unambiguous —
    # unlike slashes, where 08/22 and 22/08 are the same string to two readers.
    # So dots are converted and slashes are not.
    dotted = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(.*)$", text)
    if dotted:
        day, month, year, rest = dotted.groups()
        text = f"{year}-{int(month):02d}-{int(day):02d}{rest}"
    text = text.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # No zone given means UTC here, because that is how every project
        # announces one. Guessing the operator's zone instead would silently
        # move the whole calendar by hours.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def set_mint(
    project_id: str,
    *,
    supply: int | None = None,
    chain: str = "",
    note: str = "",
    source_url: str = "",
    mint_url: str = "",
    name: str = "",
    source: str = "manual",
    updated_by: int | None = None,
) -> None:
    pid = (project_id or "").strip()
    if not pid:
        return
    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute(
            """
            INSERT INTO project_mints
                (project_id, supply, chain, note, source_url, mint_url,
                 name, source, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                supply = excluded.supply,
                chain = excluded.chain,
                note = excluded.note,
                source_url = excluded.source_url,
                mint_url = excluded.mint_url,
                name = excluded.name,
                source = excluded.source,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (pid, supply, chain[:80], note[:400], source_url[:400],
             mint_url[:400], name[:60], source[:32], _now(), updated_by),
        )
        conn.commit()


def set_phase(
    project_id: str,
    kind: str,
    *,
    starts_at: str | None,
    price: str = "",
    note: str = "",
    label: str = "",
) -> None:
    pid = (project_id or "").strip()
    if not pid:
        return
    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute(
            """
            INSERT INTO mint_phases
                (project_id, kind, starts_at, price, note, label, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, kind) DO UPDATE SET
                starts_at = excluded.starts_at,
                price = excluded.price,
                note = excluded.note,
                label = excluded.label,
                updated_at = excluded.updated_at
            """,
            (pid, normalize_phase(kind), starts_at, price[:60], note[:200],
             (label or "")[:32], _now()),
        )
        if starts_at and normalize_phase(kind) != "tba":
            # A dateless `tba` row means "a mint was mentioned, no window
            # given". Once a real window is known it is not a second phase, it
            # is the old uncertainty — and leaving it prints an empty line
            # under the real one.
            conn.execute(
                "DELETE FROM mint_phases WHERE project_id = ? AND kind = 'tba' "
                "AND (starts_at IS NULL OR starts_at = '')", (pid,))
        conn.commit()


def clear_project(project_id: str) -> None:
    pid = (project_id or "").strip()
    if not pid:
        return
    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute("DELETE FROM mint_phases WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM project_mints WHERE project_id = ?", (pid,))
        conn.commit()


def drop_phase(project_id: str, kind: str) -> None:
    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute(
            "DELETE FROM mint_phases WHERE project_id = ? AND kind = ?",
            ((project_id or "").strip(), normalize_phase(kind)),
        )
        conn.commit()


def _phase_sort(phase: dict) -> tuple:
    """Dated phases in time order, then undated ones in their natural order."""
    rank = (KNOWN_PHASES.index(phase["kind"])
            if phase["kind"] in KNOWN_PHASES else len(KNOWN_PHASES))
    if phase["starts_at"]:
        return (0, phase["starts_at"], rank)
    return (1, "", rank)


def calendar(project_names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every project with anything known, soonest first, undated at the end.

    Sorted by the project's *next* phase rather than its first: once the GTD
    window has passed, what the reader needs from that row is the public one.
    """
    bot_db.init_db()
    names = project_names or {}
    now = _now()
    out: dict[str, dict[str, Any]] = {}
    with bot_db.db() as conn:
        for row in conn.execute(
            "SELECT project_id, supply, chain, note, source_url, mint_url, "
            "name, source, updated_at FROM project_mints"
        ):
            out[row["project_id"]] = {
                "project_id": row["project_id"],
                "project": (names.get(row["project_id"])
                            or row["name"] or row["project_id"]),
                "live": row["project_id"] in names,
                "supply": row["supply"],
                "chain": row["chain"] or "",
                "note": row["note"] or "",
                "source_url": row["source_url"] or "",
                "mint_url": row["mint_url"] or "",
                "source": row["source"] or "manual",
                "updated_at": row["updated_at"],
                "phases": [],
            }
        for row in conn.execute(
            "SELECT project_id, kind, starts_at, price, note, label FROM mint_phases"
        ):
            entry = out.setdefault(row["project_id"], {
                "project_id": row["project_id"],
                "project": names.get(row["project_id"], row["project_id"]),
                "live": row["project_id"] in names,
                "supply": None, "chain": "", "note": "", "source_url": "",
                "mint_url": "", "source": "manual", "updated_at": "", "phases": [],
            })
            entry["phases"].append({
                "kind": row["kind"],
                "label": phase_label(row["kind"], row["label"] or ""),
                "starts_at": row["starts_at"],
                "price": row["price"] or "",
                "note": row["note"] or "",
                "past": bool(row["starts_at"] and row["starts_at"] < now),
            })

    rows = list(out.values())
    for entry in rows:
        entry["phases"].sort(key=_phase_sort)
        upcoming = [p["starts_at"] for p in entry["phases"]
                    if p["starts_at"] and p["starts_at"] >= now]
        dated = [p["starts_at"] for p in entry["phases"] if p["starts_at"]]
        # Sort on what the reader still has to act on; a project whose windows
        # have all closed drops below one that has not opened yet.
        entry["next_at"] = upcoming[0] if upcoming else None
        entry["last_at"] = dated[-1] if dated else None
    rows.sort(key=lambda e: (
        0 if e["next_at"] else (1 if e["phases"] else 2),
        e["next_at"] or "",
        e["project"].lower(),
    ))
    return rows


# ---------------------------------------------------------------- proposals


def add_proposal(project_id: str, payload: dict, *, quote: str = "",
                 source_url: str = "") -> int:
    """Queue a suggestion for the operator. Never touches the calendar."""
    bot_db.init_db()
    with bot_db.db() as conn:
        cur = conn.execute(
            "INSERT INTO mint_proposals (project_id, payload, quote, source_url, "
            "status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            ((project_id or "").strip(), json.dumps(payload, ensure_ascii=False),
             (quote or "")[:600], (source_url or "")[:400], _now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def pending_proposals(limit: int = 50) -> list[dict[str, Any]]:
    bot_db.init_db()
    with bot_db.db() as conn:
        rows = conn.execute(
            "SELECT * FROM mint_proposals WHERE status = 'pending' ORDER BY id LIMIT ?",
            (int(limit),),
        ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            payload = {}
        out.append({
            "id": int(row["id"]), "project_id": row["project_id"],
            "payload": payload, "quote": row["quote"] or "",
            "source_url": row["source_url"] or "", "created_at": row["created_at"],
        })
    return out


def get_proposal(proposal_id: int) -> dict[str, Any] | None:
    pid = int(proposal_id)
    bot_db.init_db()
    with bot_db.db() as conn:
        row = conn.execute(
            """
            SELECT id, project_id, payload, quote, source_url, created_at
            FROM mint_proposals WHERE id = ? AND status = 'pending'
            """,
            (pid,),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {
        "id": int(row["id"]), "project_id": row["project_id"],
        "payload": payload, "quote": row["quote"] or "",
        "source_url": row["source_url"] or "", "created_at": row["created_at"],
    }


def decide_proposal(proposal_id: int, status: str, *, by: int | None = None) -> bool:
    """Mark a proposal accepted or rejected. Accepting does not itself publish.

    The caller writes the calendar rows first and records the decision second,
    so a crash between the two leaves a proposal still pending — a duplicate
    prompt is a nuisance, a silently dropped announcement is not.
    """
    bot_db.init_db()
    with bot_db.db() as conn:
        cur = conn.execute(
            "UPDATE mint_proposals SET status = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = 'pending'",
            (status[:16], _now(), by, int(proposal_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def apply_proposal(proposal_id: int, *, by: int | None = None) -> bool:
    """Publish a proposal into the calendar, then mark it accepted."""
    row = get_proposal(proposal_id)
    if row is None:
        return False
    payload = row["payload"] or {}
    set_mint(
        row["project_id"],
        supply=payload.get("supply"),
        chain=str(payload.get("chain") or ""),
        note=str(payload.get("note") or ""),
        source_url=row["source_url"],
        source=str(payload.get("source") or "x"),
        updated_by=by,
    )
    for phase in (payload.get("phases") or []):
        set_phase(
            row["project_id"], str(phase.get("kind") or "tba"),
            starts_at=phase.get("starts_at"),
            price=str(phase.get("price") or ""),
            note=str(phase.get("note") or ""),
            label=str(phase.get("label") or ""),
        )
    decide_proposal(proposal_id, "accepted", by=by)
    return True
