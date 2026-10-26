"""The guard that switches off a project whose route has started refusing everything.

2026-08-19, bunnyhood: `{"error":"We could not save your entry. Please try
again."}` to 1 044 rows in one hour — 96% of everything it was given, across
every user and every exit — while the bot kept feeding it. Nothing caught it,
because the health probe is a GET of the page and the page was fine: the only
place that failure exists is the row-level answer.

The rule has three parts and all three matter. Enough rows to mean something,
failures dominating them, and **one message from the route** dominating the
failures — that last one is what keeps a bad proxy hour from switching off every
project at once.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "t:t")
os.environ.setdefault("BOT_DB", str(Path(tempfile.mkdtemp(prefix="stuck_")) / "t.db"))

from bot import alerts  # noqa: E402
from bot import db as bot_db  # noqa: E402

bot_db.init_db()

ROUTE_REFUSAL = '{"error":"We could not save your entry. Please try again."}'
PROXY_ERROR = "connection error via relay-eu.proxyshard.com:8080: SSLError"


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _fill(project_id: str, *, fails: int, oks: int, detail: str) -> str:
    """Write rows and return the ISO stamp to look back from."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    for i in range(fails):
        bot_db.ensure_user(1000 + i, "u", "U")
        bot_db.record_submission(1000 + i, project_id, "h", f"0x{i:040x}", "FAIL", detail)
    for i in range(oks):
        bot_db.ensure_user(2000 + i, "u", "U")
        bot_db.record_submission(2000 + i, project_id, "h", f"0x{i + 5000:040x}", "OK", "{}")
    return since


def test_a_route_refusing_everything_is_switched_off() -> None:
    since = _fill("stuckone", fails=48, oks=2, detail=ROUTE_REFUSAL)
    found = {s["project_id"]: s for s in alerts.stuck_projects(since)}
    _ok("stuckone" in found, found)
    _ok(found["stuckone"]["failed"] == 48, found)
    _ok("could not save" in found["stuckone"]["detail"], found)

    sent: list = []
    alerts.bot_db.set_alert_marker("stuck_stuckone", "")
    keys = alerts.run_alert_checks(
        shared_proxy_count=5, send=lambda aid, text: sent.append((aid, text))
    )
    if alerts.config.ADMIN_IDS:
        _ok("stuck_stuckone" in keys, keys)
        _ok(bot_db.is_project_runtime_disabled("stuckone"), "the project must be off")
        _ok(any("could not save" in t for _a, t in sent), sent)
    else:
        # No admin configured in this environment: the check must still be safe.
        _ok(keys == [], keys)


def test_a_proxy_outage_switches_nothing_off() -> None:
    """Our transport failing is not the project's verdict."""
    since = _fill("stucktwo", fails=48, oks=2, detail=PROXY_ERROR)
    _ok(not any(s["project_id"] == "stucktwo" for s in alerts.stuck_projects(since)))
    _ok(not bot_db.is_project_runtime_disabled("stucktwo"))


def test_a_healthy_project_is_left_alone() -> None:
    since = _fill("stuckthree", fails=3, oks=60, detail=ROUTE_REFUSAL)
    _ok(not any(s["project_id"] == "stuckthree" for s in alerts.stuck_projects(since)))


def test_a_handful_of_rows_is_not_a_verdict() -> None:
    """Below the row floor nothing fires — three refusals is not a broken route."""
    since = _fill("stuckfour", fails=3, oks=0, detail=ROUTE_REFUSAL)
    _ok(not any(s["project_id"] == "stuckfour" for s in alerts.stuck_projects(since)))


def test_mixed_failures_without_one_dominant_message_are_left_alone() -> None:
    """Half the site, half our proxies — that is a diagnosis to make by hand."""
    since = _fill("stuckfive", fails=25, oks=0, detail=ROUTE_REFUSAL)
    for i in range(25):
        bot_db.ensure_user(3000 + i, "u", "U")
        bot_db.record_submission(3000 + i, "stuckfive", "h", f"0x{i + 9000:040x}", "FAIL", PROXY_ERROR)
    _ok(not any(s["project_id"] == "stuckfive" for s in alerts.stuck_projects(since)))
