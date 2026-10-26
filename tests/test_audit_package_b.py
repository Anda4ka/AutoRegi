"""Audit package B: HTTP classifier, wallet claims, bulk extra columns."""

from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="audit_b_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_RESULTS"] = str(_TMP / "r.jsonl")


def _assert(c: bool, msg: str) -> None:
    if not c:
        raise AssertionError(msg)


def test_http_status_classifier() -> None:
    from bot.http_status import (
        classify_http,
        is_permanent_http,
        is_transient_http,
    )

    _assert(is_transient_http(429), "429")
    _assert(is_transient_http(503), "503")
    _assert(is_transient_http(408), "408")
    _assert(not is_permanent_http(429), "429 not perm")
    _assert(is_permanent_http(404), "404 perm")
    _assert(is_permanent_http(405), "405 perm")
    _assert(is_permanent_http(410), "410 perm")
    _assert(not is_permanent_http(409), "409 not perm by default")
    _assert(is_permanent_http(409, treat_409_as_permanent=True), "409 opt")
    _assert(classify_http(404) == "permanent_4xx", "cls")


def test_wallet_claim_race() -> None:
    from bot import db as bot_db

    bot_db.init_db()
    u1, u2 = 901, 902
    bot_db.ensure_user(u1, "a", "a")
    bot_db.ensure_user(u2, "b", "b")
    w = "0x" + "dd" * 20
    ok1, r1 = bot_db.try_claim_wallet("proj", w, u1)
    ok2, r2 = bot_db.try_claim_wallet("proj", w, u2)
    _assert(ok1 and r1 == "", f"u1 {ok1} {r1}")
    _assert(not ok2 and r2 == "in_flight", f"u2 {ok2} {r2}")
    bot_db.release_wallet_claim("proj", w)
    ok3, r3 = bot_db.try_claim_wallet("proj", w, u2)
    _assert(ok3, f"after release {ok3} {r3}")
    bot_db.record_submission(u2, "proj", "h", w, "OK", "ok")
    bot_db.finalize_wallet_claim("proj", w)
    ok4, r4 = bot_db.try_claim_wallet("proj", w, u1)
    _assert(not ok4 and r4 == "already", f"after OK {ok4} {r4}")


def test_ok_slot_reservation_is_atomic() -> None:
    from bot import db as bot_db

    bot_db.init_db()
    uid = 903
    bot_db.ensure_user(uid, "slot", "slot")
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(
            pool.map(
                lambda _i: bot_db.try_reserve_ok_slot(uid, "slot-project", 1),
                range(8),
            )
        )
    claimed = [token for token in tokens if token]
    _assert(len(claimed) == 1, f"expected one reservation, got {claimed}")
    bot_db.release_ok_slot(claimed[0])


def test_submission_finalizes_claim_and_slot_in_one_transaction() -> None:
    from bot import db as bot_db

    bot_db.init_db()
    uid = 904
    project = "atomic-project"
    wallet = "0x" + "ee" * 20
    bot_db.ensure_user(uid, "atomic", "atomic")
    claimed, reason = bot_db.try_claim_wallet(project, wallet, uid)
    _assert(claimed and not reason, f"claim failed: {reason}")
    slot = bot_db.try_reserve_ok_slot(uid, project, 1)
    _assert(bool(slot), "slot was not reserved")

    count = bot_db.record_submission_and_finalize_claim(
        uid,
        project,
        "atomic",
        wallet,
        "OK",
        "accepted",
        claim_wallet=wallet,
        slot_reservation_id=slot,
    )
    _assert(count == 1, f"count={count}")
    with bot_db.db() as conn:
        claims = conn.execute(
            "SELECT COUNT(*) FROM wallet_claims WHERE project_id=? AND wallet=?",
            (project, wallet),
        ).fetchone()[0]
        slots = conn.execute(
            "SELECT COUNT(*) FROM ok_slot_reservations WHERE reservation_id=?",
            (slot,),
        ).fetchone()[0]
    _assert(claims == 0 and slots == 0, f"claim={claims} slot={slots}")


def test_bulk_rejects_extra_columns() -> None:
    from bot import bulk as bot_bulk

    cols = [
        ("handle", True, "handle"),
        ("wallet", True, "wallet"),
    ]
    parts = ["h", "0x" + "aa" * 20, "extra"]
    fields, err = bot_bulk.parts_to_fields(parts, cols)
    _assert(fields is None and err and "лишние", f"{fields} {err}")


