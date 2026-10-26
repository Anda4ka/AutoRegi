"""In-process admin alerts. Single bot process only."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from bot import config
from bot import db as bot_db
from bot import project_health
from bot import registry


# FAIL count in 15 min
FAIL_ALERT_THRESHOLD = int(
    __import__("os").environ.get("ADMIN_FAIL_ALERT_THRESHOLD", "20")
)
CHECK_INTERVAL_SEC = int(__import__("os").environ.get("ADMIN_ALERT_INTERVAL", "300"))
DEDUP_HOURS = 1

# A project whose route has started refusing everything.
#
# 2026-08-19: bunnyhood answered `{"error":"We could not save your entry.
# Please try again."}` to 1 044 rows in one hour — 96% of everything it was
# given, across every user and every exit — and the bot kept feeding it, because
# the health probe is a GET of the page and the page was fine. The row-level
# answer is the only place that failure is visible, so this is where it is read.
#
# What is required before switching a project off, all three:
#   * enough settled rows in the window to mean something (`STUCK_MIN_ROWS`);
#   * failures dominating them (`STUCK_FAIL_SHARE`);
#   * one **route** message dominating the failures (`STUCK_DETAIL_SHARE`) —
#     the site's own JSON, not a proxy or TLS error. Without that last one a bad
#     proxy hour would switch off every project at once.
STUCK_WINDOW_MIN = int(__import__("os").environ.get("ADMIN_STUCK_WINDOW_MIN", "15"))
STUCK_MIN_ROWS = int(__import__("os").environ.get("ADMIN_STUCK_MIN_ROWS", "40"))
STUCK_FAIL_SHARE = float(__import__("os").environ.get("ADMIN_STUCK_FAIL_SHARE", "0.8"))
STUCK_DETAIL_SHARE = float(__import__("os").environ.get("ADMIN_STUCK_DETAIL_SHARE", "0.6"))
# Off by default nowhere: set `ADMIN_STUCK_AUTOFF=0` to alert without disabling.
STUCK_AUTO_OFF = (__import__("os").environ.get("ADMIN_STUCK_AUTOFF", "1") or "").strip() != "0"


def _looks_like_the_route(detail: str) -> bool:
    """True when the failure text came from the site, not from our transport.

    The site answers JSON; a dead exit answers a Python exception. Only the
    first kind says anything about the project.
    """
    text = (detail or "").strip().lower()
    if not text:
        return False
    transport = (
        "proxy", "ssl", "timed out", "timeout", "connection", "dns",
        "max retries", "remote end closed", "certificate",
    )
    if any(word in text for word in transport):
        return False
    return text.startswith("{") or '"error"' in text or "'error'" in text


def stuck_projects(since_iso: str) -> list[dict]:
    """Projects whose rows are failing on one message from the route itself."""
    found: list[dict] = []
    for pid, mix in bot_db.project_result_mix_since(since_iso).items():
        if not pid:
            continue
        total = sum(mix.values())
        failed = mix.get("FAIL", 0) + mix.get("BAD_RESP", 0)
        if total < STUCK_MIN_ROWS or failed < total * STUCK_FAIL_SHARE:
            continue
        detail, seen, of_failed = bot_db.top_failure_detail_since(pid, since_iso)
        if not of_failed or seen < of_failed * STUCK_DETAIL_SHARE:
            continue
        if not _looks_like_the_route(detail):
            continue
        found.append(
            {
                "project_id": pid,
                "total": total,
                "failed": failed,
                "detail": detail[:300],
                "detail_count": seen,
            }
        )
    return found


_MARKER_LOCK = threading.Lock()


def _should_send(marker_key: str) -> bool:
    prev = bot_db.get_alert_marker(marker_key)
    if not prev:
        return True
    try:
        ts = datetime.fromisoformat(prev)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts >= timedelta(hours=DEDUP_HOURS)
    except Exception:
        return True


def _should_send_and_mark(marker_key: str) -> bool:
    """Atomic evaluate-and-mark: a second caller must not double-send."""
    with _MARKER_LOCK:
        if _should_send(marker_key):
            _mark(marker_key)
            return True
        return False


def _mark(marker_key: str) -> None:
    bot_db.set_alert_marker(marker_key, datetime.now(timezone.utc).isoformat())


def _close_projects_whose_gate_shut(
    send: Callable[[int, str], None],
) -> list[str]:
    """Runtime-disable a project whose own gate says intake has ended.

    Opt-in per project (`auto_close_on_gate` in `project.toml`) and deliberately
    narrow: only an **unambiguous** verdict acts — an explicit boolean or state
    string in the endpoint's own JSON, or a 404/410 on the endpoint itself.
    `project_health.probe_project` also guesses "closed" from page text, and a
    guess is fine for painting an icon in the admin panel but not for hiding a
    live project from everyone, so a text guess is reported and not acted on.

    The point is that nobody has to be watching. A campaign with a published
    deadline shuts at a time that is rarely convenient, and until it does the
    project keeps taking rows the form will refuse — every one of them spending
    a paid captcha solve to be told no.

    The disable is a runtime flag in SQLite, not an edit to `project.toml`:
    reversible from Админ → Projects → ▶ On, and it does not fight the file on
    the next `/reload`.
    """
    sent: list[str] = []
    try:
        projects = registry.list_projects()
    except Exception as exc:  # noqa: BLE001 — a broken registry is not this loop's job
        print(f"[gate] registry unavailable: {exc}", flush=True)
        return sent

    for project in projects:
        pid = str(getattr(project, "id", "") or "")
        if not pid or not bool(getattr(project, "auto_close_on_gate", False)):
            continue
        if not str(getattr(project, "health_url", "") or "").strip():
            continue
        if bot_db.is_project_runtime_disabled(pid):
            continue
        try:
            result = project_health.probe_project(project)
        except Exception as exc:  # noqa: BLE001
            print(f"[gate] probe {pid}: {exc}", flush=True)
            continue
        try:
            project_health.save_result(result)
        except Exception:
            pass
        if result.status != "closed":
            continue
        # A text-heuristic "closed" paints an icon; it does not hide a project.
        reason = str(result.detail or "")
        if not (str(result.http_status or "") in ("404", "410") or "=" in reason):
            print(f"[gate] {pid}: soft closed signal, not acting ({reason[:80]})", flush=True)
            continue
        try:
            bot_db.set_project_runtime_disabled(pid, True, admin_id=0)
        except Exception as exc:  # noqa: BLE001
            print(f"[gate] {pid}: disable failed: {exc}", flush=True)
            continue
        print(f"[gate] {pid} auto-closed: {reason[:120]}", flush=True)
        if not _should_send_and_mark(f"gate_closed_{pid}"):
            continue
        name = str(getattr(project, "name", "") or pid)
        text = (
            f"🔒 <code>{pid}</code> ({name}) — приём закрыт на стороне проекта:\n"
            f"<code>{reason[:200]}</code>\n"
            "Проект выключен автоматически (runtime). "
            "Вернуть: Админ → Projects → ▶ On."
        )
        for aid in config.ADMIN_IDS:
            try:
                send(aid, text)
            except Exception:
                pass
        sent.append(f"gate_closed_{pid}")
    return sent


def _should_alert_shared_empty(shared_proxy_count: int) -> bool:
    """True only when the pool is *supposed* to serve and has nothing to serve.

    ``get_proxies()`` is empty while the pool is intentionally off
    (``SHARED_PROXIES=0`` / SQLite ``shared_proxies_off``). That is the
    operating mode, not an accident — paging admins every ``DEDUP_HOURS``
    for it is noise. The empty-file alert stays for when the pool is on
    and the files actually have zero URLs.
    """
    if int(shared_proxy_count or 0) != 0:
        return False
    try:
        from bot import proxy_pool
    except Exception:
        return True
    try:
        return bool(proxy_pool.shared_enabled())
    except Exception:
        return True


def run_alert_checks(
    *,
    shared_proxy_count: int,
    send: Callable[[int, str], None],
) -> list[str]:
    """Evaluate conditions; send to each ADMIN_IDS via send(admin_id, text). Returns sent keys."""
    sent: list[str] = []
    if not config.ADMIN_IDS:
        return sent

    now = datetime.now(timezone.utc)
    since_15 = (now - timedelta(minutes=15)).isoformat()
    fail_n = bot_db.count_submissions_since("FAIL", since_iso=since_15)

    if fail_n >= FAIL_ALERT_THRESHOLD and _should_send_and_mark("fail_spike"):
        text = (
            f"⚠️ Alert: FAIL×{fail_n} за 15 мин "
            f"(порог {FAIL_ALERT_THRESHOLD})"
        )
        for aid in config.ADMIN_IDS:
            try:
                send(aid, text)
            except Exception:
                pass
        sent.append("fail_spike")

    since_stuck = (now - timedelta(minutes=STUCK_WINDOW_MIN)).isoformat()
    for stuck in stuck_projects(since_stuck):
        pid = stuck["project_id"]
        if bot_db.is_project_runtime_disabled(pid):
            continue
        if not _should_send_and_mark(f"stuck_{pid}"):
            continue
        share = 100 * stuck["failed"] / max(1, stuck["total"])
        action = "выключен (runtime)" if STUCK_AUTO_OFF else "оставлен включённым"
        if STUCK_AUTO_OFF:
            try:
                bot_db.set_project_runtime_disabled(pid, True, admin_id=0)
            except Exception:
                action = "выключить не удалось"
        text = (
            f"⛔️ Alert: <code>{pid}</code> отвечает отказом на "
            f"{stuck['failed']}/{stuck['total']} строк за {STUCK_WINDOW_MIN} мин "
            f"({share:.0f}%). Это ответ самого роута, не прокси:\n"
            f"<code>{stuck['detail'][:200]}</code>\n"
            f"Проект {action}. Включить: Админ → Projects → ▶ On."
        )
        for aid in config.ADMIN_IDS:
            try:
                send(aid, text)
            except Exception:
                pass
        sent.append(f"stuck_{pid}")

    sent.extend(_close_projects_whose_gate_shut(send))

    if _should_alert_shared_empty(shared_proxy_count) and _should_send_and_mark(
        "shared_empty"
    ):
        text = "⚠️ Alert: shared proxy.txt пуст — free-tier пользователи заблокированы без своих прокси."
        for aid in config.ADMIN_IDS:
            try:
                send(aid, text)
            except Exception:
                pass
        sent.append("shared_empty")

    return sent


def maybe_daily_digest(
    *,
    shared_proxy_count: int,
    own_proxy_users: int,
    send: Callable[[int, str], None],
    now_utc: datetime | None = None,
) -> bool:
    """Send once per UTC day if ADMIN_DIGEST=1. Day marker in bot_settings digest_day.

    ``now_utc`` is injectable for tests.
    """
    import os

    if os.environ.get("ADMIN_DIGEST", "").strip() not in ("1", "true", "yes"):
        return False
    # The alert thread may start before the polling bootstrap has initialized
    # the database. Make the standalone helper safe as well.
    bot_db.init_db()
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    day = now.strftime("%Y-%m-%d")
    if bot_db.get_setting("digest_day", "") == day:
        return False
    snap = bot_db.admin_stats_snapshot()
    from bot.admin_ux import format_stats

    text = (
        f"📅 Daily digest {day} UTC\n\n"
        + format_stats(
            snap,
            shared_proxies=shared_proxy_count,
            own_proxy_users=own_proxy_users,
        )
    )
    for aid in config.ADMIN_IDS:
        try:
            send(aid, text)
        except Exception:
            pass
    bot_db.set_setting("digest_day", day)
    return True


def start_alert_thread(
    *,
    get_shared_count: Callable[[], int],
    send: Callable[[int, str], None],
    get_own_proxy_users: Callable[[], int] | None = None,
) -> threading.Thread:
    """Background loop: FAIL/shared alerts + daily digest (ADMIN_DIGEST=1)."""

    def _own() -> int:
        if get_own_proxy_users is None:
            return 0
        try:
            return int(get_own_proxy_users())
        except Exception:
            return 0

    def _loop() -> None:
        while True:
            try:
                shared_n = get_shared_count()
                run_alert_checks(
                    shared_proxy_count=shared_n,
                    send=send,
                )
                # Suggest→draft→probe pipeline (pilot; SUGGEST_PIPELINE=1).
                # Self-gating: env off, busy, no inbox ⇒ returns immediately.
                from bot import suggest_pipeline
                suggest_pipeline.maybe_sweep(send)
                # NFT Trencher board feed → mint calendar + T-1h reminders.
                # Self-gating too: MINTS_ENABLED=0 or no NFTT_API_KEY ⇒ noop.
                from bot import nftt
                nftt.sweep()
                # Re-check every interval so long-lived bots send next UTC days
                maybe_daily_digest(
                    shared_proxy_count=shared_n,
                    own_proxy_users=_own(),
                    send=send,
                )
            except Exception as e:
                print(f"[alerts] check error: {e}", flush=True)
            time.sleep(CHECK_INTERVAL_SEC)

    t = threading.Thread(target=_loop, name="admin-alerts", daemon=True)
    t.start()
    return t
