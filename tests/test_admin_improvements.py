"""Tests for admin helpers (bot.db / rate_limit / alerts)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="admin_imp_"))
_DB = _TMP / "t.db"
_PDIR = _TMP / "prox"
_PDIR.mkdir(parents=True, exist_ok=True)
os.environ["BOT_DB"] = str(_DB)
os.environ["USER_PROXY_DIR"] = str(_PDIR)
os.environ["BOT_TOKEN"] = "t:t"
os.environ["FREE_OK_LIMIT"] = "10"
os.environ["ADMIN_DIGEST"] = "0"

for m in list(sys.modules):
    if m == "bot" or m.startswith("bot.") or m == "handlers" or m.startswith("handlers."):
        del sys.modules[m]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import admin_ux  # noqa: E402
from bot import rate_limit as rl  # noqa: E402
from bot import user_proxies as up  # noqa: E402
from bot import alerts as bot_alerts  # noqa: E402
import bot.db as dbmod  # noqa: E402

config.DB_PATH = _DB
config.USER_PROXY_DIR = _PDIR
dbmod.DB_PATH = _DB
bot_db.init_db()


def _ok(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_ban_and_reset() -> None:
    uid = 900001
    bot_db.ensure_user(uid, "tester", "T")
    _ok(not bot_db.is_banned(uid), "not banned")
    bot_db.set_banned(uid, True, admin_id=1, reason="spam")
    _ok(bot_db.is_banned(uid), "banned")
    # seed stats
    bot_db.record_submission(uid, "demoh", "h", "0x" + "a" * 40, "OK", "ok")
    bot_db.record_submission(uid, "demoh", "h2", "0x" + "b" * 40, "OK", "ok")
    _ok(bot_db.get_ok_count(uid, "demoh") == 2, "ok count 2")
    n_sub_before = bot_db.list_recent_submissions(uid, 50)
    bot_db.reset_ok_count(uid, None, admin_id=1)
    _ok(bot_db.get_ok_count(uid, "demoh") == 0, "reset stats")
    n_sub_after = bot_db.list_recent_submissions(uid, 50)
    _ok(len(n_sub_after) == len(n_sub_before), "history kept")
    bot_db.set_banned(uid, False, admin_id=1)
    _ok(not bot_db.is_banned(uid), "unbanned")


def test_find_user_and_stats() -> None:
    uid = 900002
    bot_db.ensure_user(uid, "LookupMe", "L")
    _ok(bot_db.find_user_id("900002") == uid, "by id")
    _ok(bot_db.find_user_id("@LookupMe") == uid, "by username")
    _ok(bot_db.find_user_id("lookupme") == uid, "case insensitive")
    bot_db.record_submission(uid, "demoh", "x", "0x" + "c" * 40, "FAIL", "proxy err")
    snap = bot_db.admin_stats_snapshot()
    _ok(snap["users_total"] >= 1, "users")
    _ok("funnel" in snap, "funnel")
    errs = bot_db.list_recent_errors(10)
    _ok(any(e.get("status") == "FAIL" for e in errs), "errors list")
    card = admin_ux.format_user_card(uid)
    _ok("900002" in card and "FAIL" in card, "card")


def test_limits_invariant_via_db() -> None:
    ok, _ = bot_db.set_free_limit(10, admin_id=1)
    _ok(ok, "free 10")
    # The own-proxy tier was removed on 2026-09-09: the knob must not exist.
    _ok(not hasattr(bot_db, "set_own_limit"), "set_own_limit removed")
    _ok(not hasattr(bot_db, "get_own_limit"), "get_own_limit removed")


def test_rate_limit() -> None:
    uid = 900003
    rl.reset_user(uid)
    # force tight limit by temporarily changing - use many allows on bulk_file
    # default bulk_file is 5/hour — 6th fails
    for i in range(5):
        ok, _ = rl.allow(uid, "bulk_file")
        _ok(ok, f"allow {i}")
    ok, msg = rl.allow(uid, "bulk_file")
    _ok(not ok and msg, "6th blocked")


def test_admin_ux_stats_no_secret() -> None:
    snap = bot_db.admin_stats_snapshot()
    text = admin_ux.format_stats(snap, shared_proxies=3, own_proxy_users=1)
    _ok("Stats" in text or "📊" in text, "stats text")
    _ok("password" not in text.lower(), "no password")


def test_own_proxy_count_path() -> None:
    uid = 900004
    bot_db.ensure_user(uid, "p", "p")
    ok, _, n = up.save_user_proxies_from_bytes(uid, b"1.2.3.4:80:u:p\n")
    _ok(ok and n == 1, "saved")
    _ok(up.has_own_proxies(uid), "has own")


def test_is_admin_fail_closed() -> None:
    """Empty ADMIN_IDS denies admin unless ALLOW_OPEN_ADMIN."""
    old_ids = set(config.ADMIN_IDS)
    old_open = config.ALLOW_OPEN_ADMIN
    try:
        config.ADMIN_IDS.clear()
        config.ALLOW_OPEN_ADMIN = False
        # mimic tgbot logic
        def is_admin(uid: int) -> bool:
            if not config.ADMIN_IDS:
                return bool(getattr(config, "ALLOW_OPEN_ADMIN", False))
            return uid in config.ADMIN_IDS

        _ok(is_admin(1) is False, "fail closed")
        config.ALLOW_OPEN_ADMIN = True
        _ok(is_admin(1) is True, "open admin dev")
        config.ALLOW_OPEN_ADMIN = False
        config.ADMIN_IDS.add(42)
        _ok(is_admin(42) is True and is_admin(1) is False, "only listed")
    finally:
        config.ADMIN_IDS.clear()
        config.ADMIN_IDS.update(old_ids)
        config.ALLOW_OPEN_ADMIN = old_open


def test_daily_digest_day_dedup() -> None:
    """Second call same UTC day is no-op; new day sends again (shipped maybe_daily_digest)."""
    from datetime import datetime, timezone

    os.environ["ADMIN_DIGEST"] = "1"
    # ensure ADMIN_IDS so send loop runs
    if not config.ADMIN_IDS:
        config.ADMIN_IDS.add(1)
    bot_db.set_setting("digest_day", "")  # clear marker
    sent: list[str] = []

    def send(aid: int, text: str) -> None:
        sent.append(text)

    day1 = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 20, 0, 10, tzinfo=timezone.utc)

    r1 = bot_alerts.maybe_daily_digest(
        shared_proxy_count=1,
        own_proxy_users=0,
        send=send,
        now_utc=day1,
    )
    _ok(r1 is True, "first day should send")
    _ok(len(sent) >= 1, "message sent")
    n_after_first = len(sent)

    r2 = bot_alerts.maybe_daily_digest(
        shared_proxy_count=1,
        own_proxy_users=0,
        send=send,
        now_utc=day1,
    )
    _ok(r2 is False, "same day no-op")
    _ok(len(sent) == n_after_first, "no extra send same day")
    _ok(bot_db.get_setting("digest_day", "") == "2026-07-19", "marker day1")

    r3 = bot_alerts.maybe_daily_digest(
        shared_proxy_count=2,
        own_proxy_users=1,
        send=send,
        now_utc=day2,
    )
    _ok(r3 is True, "new day should send")
    _ok(len(sent) > n_after_first, "new message on day2")
    _ok(bot_db.get_setting("digest_day", "") == "2026-07-20", "marker day2")

    # alert loop must call digest (shipped start_alert_thread body)
    src = Path(ROOT / "bot" / "alerts.py").read_text(encoding="utf-8")
    # after def start_alert_thread, _loop must invoke maybe_daily_digest
    idx = src.find("def start_alert_thread")
    _ok(idx >= 0, "start_alert_thread present")
    loop_body = src[idx:]
    _ok("maybe_daily_digest(" in loop_body, "digest inside alert thread")
    # banned proxy clear gate in the proxy callback (handlers/profile.py since
    # the create_bot split)
    prof = Path(ROOT / "handlers" / "profile.py").read_text(encoding="utf-8")
    proxy_cb = prof[prof.find("def on_proxy_cb") :]
    _ok("is_banned" in proxy_cb.split("proxy:clear")[0], "ban before clear")


def main() -> None:
    tests = [
        test_ban_and_reset,
        test_find_user_and_stats,
        test_limits_invariant_via_db,
        test_rate_limit,
        test_admin_ux_stats_no_secret,
        test_own_proxy_count_path,
        test_is_admin_fail_closed,
        test_daily_digest_day_dedup,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
