"""Unit tests for limits, proxy parse, resolve, and batch caps."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate DB + proxy dir before importing bot modules that load .env
_TMP = Path(tempfile.mkdtemp(prefix="limits_proxy_test_"))
_DB = _TMP / "test.db"
_PDIR = _TMP / "user_proxies"
_PDIR.mkdir(parents=True, exist_ok=True)
os.environ["BOT_DB"] = str(_DB)
os.environ["USER_PROXY_DIR"] = str(_PDIR)
os.environ["FREE_OK_LIMIT"] = "10"
# Pin the subscriber tier too: this module asserts the ladder «subscriber is not
# lowered by uploading own proxies», which only holds while subscriber >= own.
# Left unset, it inherited whatever the operator's .env said (lowered to 50 on
# 2026-08-14) and the mode assertion broke on a config change, not a code one.
os.environ["SUBSCRIBER_OK_LIMIT"] = "100"
# Tier assertions must never read a cached Caps (also set in tests/conftest.py
# for pytest runs; this file is also executed standalone).
os.environ["LIMITS_CACHE_TTL"] = "0"
# Avoid loading real .env BOT_TOKEN issues — dotenv still loads but we override paths after
os.environ["BOT_TOKEN"] = "test:token"
os.environ["USER_OK_LIMITS"] = "333333333:1000"

# Force re-import config with new env if already imported
for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import limits as bot_limits  # noqa: E402
from bot import subscribers as sub  # noqa: E402
from bot import user_proxies as up  # noqa: E402
from bot import bulk as bot_bulk  # noqa: E402

# Ensure config paths match test isolation
config.DB_PATH = _DB
config.USER_PROXY_DIR = _PDIR
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]
# db module bound DB_PATH at import
import bot.db as dbmod  # noqa: E402

dbmod.DB_PATH = _DB

bot_db.init_db()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_free_own_vip_limits() -> None:
    uid_free = 111001
    uid_own = 111002
    uid_vip_hard = 333333333
    uid_vip_db = 111003

    # free
    _assert(not up.has_own_proxies(uid_free), "no own yet")
    _assert(bot_limits.effective_ok_limit(uid_free) == bot_db.get_free_limit(), "free limit")
    _assert(bot_limits.limit_mode(uid_free) == "free", "mode free")

    # own proxies
    ok, msg, n = up.save_user_proxies_from_bytes(
        uid_own,
        b"1.2.3.4:8080:user:pass\n",
    )
    _assert(ok and n == 1, f"save own: {msg}")
    _assert(up.has_own_proxies(uid_own), "has own")
    # Own proxies grant no limit anymore (operator rule, 2026-09-09):
    # they stay a traffic source only, so the user keeps the free tier.
    _assert(bot_limits.effective_ok_limit(uid_own) == bot_db.get_free_limit(), "no own limit")
    _assert(bot_limits.limit_mode(uid_own) == "free", "mode free with own proxies")

    # hardcode VIP
    _assert(bot_limits.effective_ok_limit(uid_vip_hard) == 1000, "vip hardcode 1000")
    _assert(bot_limits.is_vip(uid_vip_hard), "is vip")

    # DB VIP override wins
    bot_db.set_user_limit_override(uid_vip_db, 500, admin_id=1)
    _assert(bot_limits.effective_ok_limit(uid_vip_db) == 500, "db vip 500")
    bot_db.del_user_limit_override(uid_vip_db)
    _assert(bot_limits.effective_ok_limit(uid_vip_db) == bot_db.get_free_limit(), "after del free")

    # hardcode VIP with db override
    bot_db.set_user_limit_override(uid_vip_hard, 2000, admin_id=1)
    _assert(bot_limits.effective_ok_limit(uid_vip_hard) == 2000, "db overrides hardcode")
    bot_db.del_user_limit_override(uid_vip_hard)
    _assert(bot_limits.effective_ok_limit(uid_vip_hard) == 1000, "back to hardcode")


def test_subscriber_shared_limit() -> None:
    # Dedicated uid — do not reuse resolve-test ids (33300x)
    uid_sub = 444001
    # empty → free
    sub.load_subscribers(_TMP / "no_such_subs.txt")
    _assert(not sub.is_subscriber(uid_sub), "not sub yet")
    _assert(bot_limits.effective_ok_limit(uid_sub) == bot_db.get_free_limit(), "free")
    _assert(bot_limits.limit_mode(uid_sub) == "free", "mode free")

    path = _TMP / "subs.txt"
    path.write_text(f"# comment\n{uid_sub}\n999888777\n", encoding="utf-8")
    n = sub.load_subscribers(path)
    _assert(n == 2, f"loaded 2 got {n}")
    _assert(sub.is_subscriber(uid_sub), "is sub")
    _assert(
        bot_limits.effective_ok_limit(uid_sub) == int(config.SUBSCRIBER_OK_LIMIT),
        "subscriber limit",
    )
    _assert(bot_limits.limit_mode(uid_sub) == "subscriber", "mode subscriber")

    # Own proxies change the pool but must not touch the subscriber's slots.
    ok, msg, _ = up.save_user_proxies_from_bytes(uid_sub, b"1.2.3.4:8080:u:p\n")
    _assert(ok, msg)
    _assert(
        bot_limits.effective_ok_limit(uid_sub) == int(config.SUBSCRIBER_OK_LIMIT),
        "subscriber limit unchanged by own proxies",
    )
    _assert(bot_limits.limit_mode(uid_sub) == "subscriber", "mode subscriber")

    # VIP beats all
    bot_db.set_user_limit_override(uid_sub, 777, admin_id=1)
    _assert(bot_limits.effective_ok_limit(uid_sub) == 777, "vip wins")
    bot_db.del_user_limit_override(uid_sub)


def test_admin_free_own_invariant() -> None:
    ok, _ = bot_db.set_free_limit(10, admin_id=1)
    _assert(ok, "set free 10")
    # No own_limit coupling anymore (own tier removed 2026-09-09): free_limit
    # stands alone and the old knob is gone from the DB API entirely.
    _assert(not hasattr(bot_db, "set_own_limit"), "set_own_limit removed")
    _assert(not hasattr(bot_db, "get_own_limit"), "get_own_limit removed")


def test_proxy_parse_reject_all() -> None:
    # good
    r = up.parse_proxy_text("1.1.1.1:80:u:p\nsocks5:2.2.2.2:1080:u:p\n")
    _assert(r.ok and len(r.urls) == 2, "two good lines")
    _assert(r.urls[0].startswith("http://"), "http default")
    _assert(r.urls[1].startswith("socks5://"), "socks5")

    # one bad → not ok
    r2 = up.parse_proxy_text("1.1.1.1:80:u:p\nbadline\n")
    _assert(not r2.ok and r2.errors, "reject with bad line")

    # save must not overwrite on bad
    uid = 222001
    ok, _, n = up.save_user_proxies_from_bytes(uid, b"9.9.9.9:1:a:b\n")
    _assert(ok and n == 1, "first good save")
    path = up.user_proxy_path(uid)
    before = path.read_text(encoding="utf-8")
    ok2, msg2, _ = up.save_user_proxies_from_bytes(uid, b"good:1:a:b\nNOTVALID\n")
    _assert(not ok2, f"bad file rejected: {msg2}")
    after = path.read_text(encoding="utf-8")
    _assert(before == after, "previous file untouched")

    # socks without auth rejected
    r3 = up.parse_proxy_text("socks5:1.1.1.1:1080\n")
    _assert(not r3.ok, "socks needs auth")


def test_resolve_priority() -> None:
    uid = 333001
    shared = ["http://u:p@10.0.0.1:8080"]
    # no own → shared
    res = up.resolve_proxies(uid, allow_direct=True, shared=shared, is_vip_user=False)
    _assert(res.source == "shared", f"expected shared got {res.source}")

    up.save_user_proxies_from_bytes(uid, b"8.8.8.8:80:u:p\n")
    res2 = up.resolve_proxies(uid, allow_direct=True, shared=shared, is_vip_user=False)
    _assert(res2.source == "own", "own wins")
    _assert(res2.proxies and "8.8.8.8" in res2.proxies[0], "own url")

    # free empty shared no own → none
    uid2 = 333002
    res3 = up.resolve_proxies(uid2, allow_direct=True, shared=[], is_vip_user=False)
    _assert(res3.source == "none", "free cannot direct")

    # Policy: no direct path even for VIP + allow_direct / prefer_direct
    res4 = up.resolve_proxies(uid2, allow_direct=True, shared=[], is_vip_user=True)
    _assert(res4.source == "none", "vip no direct either")

    res4b = up.resolve_proxies(
        uid2, allow_direct=True, shared=shared, is_vip_user=True, prefer_direct=True
    )
    _assert(res4b.source == "shared", "prefer_direct ignored → still shared")
    _assert(res4b.proxies == shared, "shared pool used")

    res5 = up.resolve_proxies(uid2, allow_direct=False, shared=[], is_vip_user=True)
    _assert(res5.source == "none", "vip empty shared → none")

    # corrupt own file: must NOT fall back to shared (audit P0)
    uid3 = 333003
    path = up.user_proxy_path(uid3)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-a-valid-proxy-line\n", encoding="utf-8")
    _assert(up.own_proxy_file_exists(uid3), "file exists")
    _assert(not up.has_own_proxies(uid3), "has_own false for corrupt")
    res6 = up.resolve_proxies(uid3, allow_direct=True, shared=shared, is_vip_user=False)
    _assert(res6.source == "none", f"corrupt must be none not {res6.source}")


def test_bulk_cap_and_partial() -> None:
    cols = [
        ("handle", True, "twitter handle"),
        ("wallet", True, "wallet 0x…"),
    ]

    def validate(fields: dict) -> str | None:
        w = fields.get("wallet") or ""
        if not (w.startswith("0x") and len(w) == 42):
            return "bad wallet"
        return None

    good_w = "0x" + "a" * 40
    text = f"h1|{good_w}\nbadonly\nh2|{good_w}\n"
    parsed = bot_bulk.parse_bulk_text(text, columns=cols, validate=validate, max_lines=100)
    _assert(len(parsed.valid) == 2, f"valid={len(parsed.valid)}")
    _assert(len(parsed.invalid) >= 1, "has invalid")

    # cap: max_lines
    lines = "\n".join(f"h{i}|{good_w}" for i in range(5))
    p2 = bot_bulk.parse_bulk_text(lines, columns=cols, validate=validate, max_lines=3)
    # first 3 data lines ok, 4th hits max with error
    _assert(len(p2.valid) == 3, f"cap valid {len(p2.valid)}")


def test_bulk_tier_caps() -> None:
    """VIP gets higher line/byte caps; free stays at defaults."""
    free_uid = 900001
    bot_db.ensure_user(free_uid, "free_user", "Free")
    bot_db.del_user_limit_override(free_uid)

    free_lines = bot_limits.effective_bulk_max_lines(free_uid)
    free_bytes = bot_limits.effective_bulk_max_bytes(free_uid)
    _assert(free_lines == config.BULK_MAX_LINES, f"free lines {free_lines}")
    _assert(free_bytes == config.BULK_MAX_BYTES, f"free bytes {free_bytes}")

    vip_uid = 333333333
    vip_lines = bot_limits.effective_bulk_max_lines(vip_uid)
    vip_bytes = bot_limits.effective_bulk_max_bytes(vip_uid)
    _assert(vip_lines == config.BULK_VIP_MAX_LINES, f"vip lines {vip_lines}")
    _assert(vip_bytes == config.BULK_VIP_MAX_BYTES, f"vip bytes {vip_bytes}")
    _assert(vip_lines > free_lines, "vip lines > free")
    _assert(vip_bytes > free_bytes, "vip bytes > free")

    # decode respects max_bytes
    blob = b"x" * (config.BULK_MAX_BYTES + 10)
    _t, err = bot_bulk.decode_txt_bytes(blob, max_bytes=config.BULK_MAX_BYTES)
    _assert(err is not None and "большой" in err, f"free size reject: {err}")
    _t2, err2 = bot_bulk.decode_txt_bytes(blob, max_bytes=config.BULK_VIP_MAX_BYTES)
    _assert(err2 is None and _t2 is not None, f"vip size accept: {err2}")

    # DB VIP override also gets VIP bulk caps
    ovr_uid = 900002
    bot_db.ensure_user(ovr_uid, "vip_ovr", "Vip")
    bot_db.set_user_limit_override(ovr_uid, 1000)
    _assert(bot_limits.is_vip(ovr_uid), "override is vip")
    _assert(
        bot_limits.effective_bulk_max_lines(ovr_uid) == config.BULK_VIP_MAX_LINES,
        "override vip lines",
    )


def test_mask_no_password() -> None:
    m = up.mask_proxy_url("http://secret:pass@1.2.3.4:8080")
    _assert("secret" not in m and "pass" not in m, f"leaked: {m}")
    _assert("1.2.3.4" in m, m)


def main() -> None:
    tests = [
        test_free_own_vip_limits,
        test_subscriber_shared_limit,
        test_admin_free_own_invariant,
        test_proxy_parse_reject_all,
        test_resolve_priority,
        test_bulk_cap_and_partial,
        test_bulk_tier_caps,
        test_mask_no_password,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
