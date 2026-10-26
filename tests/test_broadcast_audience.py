"""Admin broadcast audiences: everyone / subscribers / paid plans.

The broadcast used to hit every user unconditionally. It now asks who the
message is for, so a limit change aimed at `active_subscribers.txt` does not
land in the inbox of people it does not concern.

Invariants under test:
  * every audience is intersected with the bot's own user table (an id that
    never started the bot cannot receive anything);
  * banned users are never recipients;
  * an unknown / legacy key ("1") still means «everyone».
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="bcast_aud_"))
_DB = _TMP / "t.db"
_SUBS = _TMP / "active_subscribers.txt"
_SUBS.write_text("101\n102\n999999\n# comment\n\n", encoding="utf-8")

os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "t:t"
os.environ["SUBSCRIBERS_FILE"] = str(_SUBS)
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")

# Drop `tgbot` together with `bot.*`: this module repoints the state paths, and
# a `tgbot` left over from an earlier test module would keep its old `bot.db`
# binding — poisoning every module that imports tgbot after this one (whole-suite
# pytest runs, where the per-module CI runner would never notice).
for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import subscribers as bot_subscribers  # noqa: E402

config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]

import tgbot  # noqa: E402


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _seed() -> None:
    bot_db.init_db()
    for uid in (101, 102, 103, 104, 105):
        bot_db.ensure_user(uid, f"u{uid}", f"U{uid}")
    # 105 is banned: never a recipient, whatever the audience.
    bot_db.set_banned(105, True)
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    bot_db.upsert_subscription(103, "basic", future, source="test")
    bot_db.upsert_subscription(104, "basic", past, source="test")
    bot_subscribers.load_subscribers()


def test_subscribers_audience_is_file_intersect_known_users() -> None:
    _seed()
    ids, label = tgbot.broadcast_audience("subs")
    _ok(label == "Подписчики", label)
    # 101 and 102 are in the file AND known to the bot; 999999 is in the file
    # but never started the bot, so it is not a recipient.
    _ok(sorted(ids) == [101, 102], sorted(ids))
    _ok(999999 not in ids, "unknown id must not be a recipient")


def test_paid_audience_only_active_plans() -> None:
    _seed()
    ids, label = tgbot.broadcast_audience("paid")
    _ok(label == "Платные тарифы", label)
    _ok(sorted(ids) == [103], sorted(ids))  # 104's plan expired


def test_all_and_legacy_key() -> None:
    _seed()
    everyone, label = tgbot.broadcast_audience("all")
    _ok(label == "Все", label)
    legacy, _ = tgbot.broadcast_audience("1")
    unknown, _ = tgbot.broadcast_audience("nonsense")
    _ok(sorted(legacy) == sorted(everyone), "legacy key must mean everyone")
    _ok(sorted(unknown) == sorted(everyone), "unknown key must mean everyone")
    known = set(bot_db.list_all_user_ids(exclude_banned=True))
    _ok(set(everyone) == known, "audience must equal known non-banned users")


def test_banned_never_receive() -> None:
    _seed()
    bot_db.set_banned(101, True)
    try:
        for key in tgbot.BROADCAST_AUDIENCES:
            ids, _label = tgbot.broadcast_audience(key)
            _ok(101 not in ids, f"banned user reached via audience {key}")
    finally:
        bot_db.set_banned(101, False)


def test_audience_keys_have_buttons() -> None:
    # Every key offered in the UI must resolve to a labelled audience.
    for key in tgbot.BROADCAST_AUDIENCES:
        ids, label = tgbot.broadcast_audience(key)
        _ok(isinstance(ids, list), key)
        _ok(bool(label), f"audience {key} has no label")


def main() -> None:
    tests = [
        test_subscribers_audience_is_file_intersect_known_users,
        test_paid_audience_only_active_plans,
        test_all_and_legacy_key,
        test_banned_never_receive,
        test_audience_keys_have_buttons,
    ]
    failed = 0
    for tfn in tests:
        try:
            tfn()
            print(f"PASS {tfn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {tfn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
