"""Bot wallet dedup: global (project+wallet) and local (user+project+wallet)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="dedup_test_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_RESULTS"] = str(_TMP / "r.jsonl")
os.environ["USER_DATA_DIR"] = str(_TMP / "ud")
os.environ["USER_PROXY_DIR"] = str(_TMP / "up")


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_local_and_global() -> None:
    from bot import db as bot_db

    bot_db.init_db()
    u1, u2 = 424242, 434343
    bot_db.ensure_user(u1, "a", "a")
    bot_db.ensure_user(u2, "b", "b")
    w = "0x" + "ab" * 20

    _assert(bot_db.already_registered_wallet("proj", w) is None, "empty global")
    bot_db.record_submission(u1, "proj", "h1", w, "OK", "ok")

    g = bot_db.already_registered_wallet("proj", w)
    _assert(g is not None and g["status"] == "OK", "global finds u1")
    _assert(g.get("scope") == "global", "scope global")
    _assert(g.get("by_user_id") == u1, "by_user")

    # other user blocked by global
    g2 = bot_db.already_registered_wallet("proj", w)
    _assert(g2 is not None, "u2 also blocked globally")

    # local-only for u2: no row for u2
    loc = bot_db.already_registered_wallet("proj", w, user_id=u2)
    _assert(loc is None, "local u2 empty")
    loc1 = bot_db.already_registered_wallet("proj", w, user_id=u1)
    _assert(loc1 is not None, "local u1 hit")

    _assert(
        bot_db.already_registered_wallet("other", w) is None,
        "other project free",
    )

    w2 = "0x" + "cd" * 20
    bot_db.record_submission(u1, "proj", "h2", w2, "FAIL", "net")
    _assert(
        bot_db.already_registered_wallet("proj", w2) is None,
        "FAIL not dedup",
    )


def test_tx_hash_is_not_a_wallet() -> None:
    """A 64-hex tx hash must never pass the dedup/claim gate (AGENTS 16.3)."""
    from bot import db as bot_db

    bot_db.init_db()
    u = 616161
    bot_db.ensure_user(u, "a", "a")
    tx = "0x" + "ab" * 32  # 64 hex — a transaction hash
    bot_db.record_submission(u, "proj", "h", tx, "OK", "ok")
    _assert(
        bot_db.already_registered_wallet("proj", tx) is None,
        "a stored tx-hash row must not dedup a real wallet",
    )
    ok, reason = bot_db.try_claim_wallet("proj", tx, u)
    _assert(ok and reason == "", "tx hash is not claimable either")
    _assert(
        tx not in bot_db.wallets_already_registered("proj", [tx]),
        "batch dedup skips tx hashes",
    )


def test_subscriber_limit_zero_disables_tier() -> None:
    """SUBSCRIBER_OK_LIMIT=0 must disable the tier, not grant 100 (AGENTS 16.5)."""
    from bot import config
    from bot import group_tier
    from unittest import mock

    with mock.patch.object(config, "SUBSCRIBER_OK_LIMIT", 0), \
         mock.patch.object(config, "SUBSCRIBER_GROUP_OK_LIMIT", 0):
        _assert(group_tier.subscriber_ok_limit() == 0, "0 passes through")
        _assert(group_tier.member_ok_limit() == 0, "group tier disabled too")
    with mock.patch.object(config, "SUBSCRIBER_OK_LIMIT", None):
        _assert(group_tier.subscriber_ok_limit() == 100, "None falls back")
    with mock.patch.object(config, "SUBSCRIBER_OK_LIMIT", 50):
        _assert(group_tier.subscriber_ok_limit() == 50, "value respected")


def test_batch_global() -> None:
    from bot import db as bot_db

    bot_db.init_db()
    u1, u2 = 515151, 525252
    bot_db.ensure_user(u1, "t", "t")
    bot_db.ensure_user(u2, "t2", "t2")
    w1 = "0x" + "11" * 20
    w2 = "0x" + "22" * 20
    w3 = "0x" + "33" * 20
    bot_db.record_submission(u1, "p", "a", w1, "OK", "")
    bot_db.record_submission(u2, "p", "b", w2, "ALREADY", "")
    got = bot_db.wallets_already_registered("p", [w1, w2, w3, w1])
    _assert(w1 in got and w2 in got and w3 not in got, f"batch {got}")
    # local batch for u2 only sees w2
    loc = bot_db.wallets_already_registered("p", [w1, w2, w3], user_id=u2)
    _assert(w2 in loc and w1 not in loc, f"local batch {loc}")


def test_prepare_skips_global_dup() -> None:
    from bot import db as bot_db
    from bot import submit_queue as q
    from bot import user_data as ud

    bot_db.init_db()
    q.init_schema()
    u1, u2 = 616161, 626262
    bot_db.ensure_user(u1, "t", "t")
    bot_db.ensure_user(u2, "t2", "t2")
    w_old = "0x" + "aa" * 20
    w_new = "0x" + "bb" * 20
    # registered by *other* user
    bot_db.record_submission(u1, "democ", "", w_old, "OK", "")

    path = ud.user_data_path(u2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"oldHandle|{w_old}\nnewHandle|{w_new}\n",
        encoding="utf-8",
    )

    class FakeProject:
        id = "democ"
        name = "G"
        bulk_columns = [("wallet", True, "w")]
        auto_tweet_urls = 0

        def parse(self, text: str):
            t = (text or "").strip()
            if t.startswith("0x") and len(t) >= 42:
                return {"wallet": t.lower(), "handle": ""}
            if "|" in t:
                a, b = t.split("|", 1)
                return {"handle": a.lstrip("@"), "wallet": b.strip().lower()}
            return None

        def validate(self, fields: dict):
            return None

    rows, errs, skip = q.prepare_job_rows(u2, FakeProject())
    _assert(skip == 1, f"skip={skip} (global from u1)")
    _assert(len(rows) == 1, f"rows={rows}")
    _assert(rows[0]["wallet"] == w_new, "only new")


def main() -> None:
    test_local_and_global()
    print("PASS test_local_and_global")
    test_batch_global()
    print("PASS test_batch_global")
    test_prepare_skips_global_dup()
    print("PASS test_prepare_skips_global_dup")
    print("ALL 3 PASSED")


if __name__ == "__main__":
    main()
