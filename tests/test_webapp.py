"""Mini app prototype (`webapp/`).

`initData` verification is the entire security boundary: the page runs on the
user's phone and that signed string is the only proof of who is asking. If it
can be forged, replayed forever, or bypassed, the app hands one user another
user's wallets. So the tests below spend their weight there — a valid string
passes, and every way of breaking it fails.

The state layer is checked for the other half of the promise: it opens the
bot's database read-only, so the prototype cannot spend a slot or damage a job
no matter what it is asked to do.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "t:t")
os.environ.setdefault("BOT_DB", str(Path(tempfile.mkdtemp(prefix="webapp_")) / "t.db"))

from webapp import ratelimit  # noqa: E402
from webapp import state as webstate  # noqa: E402
from webapp.auth import AuthError, user_id_from, verify_init_data  # noqa: E402

TOKEN = "123456:AAtest-token-value"


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _raises(fn, msg: str) -> None:
    try:
        fn()
    except AuthError:
        return
    raise AssertionError(msg)


def sign(fields: dict, token: str = TOKEN) -> str:
    """Build an initData string the way Telegram builds it."""
    data = {k: v for k, v in fields.items() if k != "hash"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def good_fields(user_id: int = 111111111, age: int = 10) -> dict:
    return {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAEtest",
        "user": json.dumps({"id": user_id, "first_name": "Op", "username": "op"}),
    }


# ------------------------------------------------------------------ initData


def test_a_properly_signed_string_verifies() -> None:
    data = verify_init_data(sign(good_fields()), TOKEN)
    _ok(data["user"]["id"] == 111111111, data)
    _ok(user_id_from(sign(good_fields(7)), TOKEN) == 7)


def test_a_forged_hash_is_refused() -> None:
    raw = sign(good_fields())
    tampered = raw[:-1] + ("0" if raw[-1] != "0" else "1")
    _raises(lambda: verify_init_data(tampered, TOKEN), "a wrong hash must not pass")


def test_another_bots_token_cannot_sign_for_this_one() -> None:
    """The signature is what binds the data to *our* bot."""
    other = sign(good_fields(), token="999999:someone-elses-bot")
    _raises(lambda: verify_init_data(other, TOKEN), "a foreign token must not pass")


def test_changing_the_user_after_signing_is_refused() -> None:
    """The attack that matters: keep a real signature, swap whose data it is."""
    fields = good_fields(user_id=111111111)
    raw = sign(fields)
    swapped = raw.replace("111111111", "222222222")
    _ok(swapped != raw, "the id must actually appear in the payload")
    _raises(lambda: verify_init_data(swapped, TOKEN), "an edited user must not pass")


def test_an_old_signature_expires() -> None:
    """A valid signature never expires on its own — freshness is what bounds a leak."""
    stale = sign(good_fields(age=48 * 3600))
    _raises(lambda: verify_init_data(stale, TOKEN), "a day-old initData must not pass")
    # Inside the window the very same string is fine.
    fresh = sign(good_fields(age=60))
    _ok(verify_init_data(fresh, TOKEN)["user"]["id"] == 111111111)


def test_missing_pieces_are_refused() -> None:
    for label, raw in (
        ("empty", ""),
        ("no hash", urlencode(good_fields())),
        ("no user", sign({"auth_date": str(int(time.time()))})),
        ("no auth_date", sign({"user": json.dumps({"id": 1})})),
        ("user is not json", sign({**good_fields(), "user": "not-json"})),
        ("user has no id", sign({**good_fields(), "user": json.dumps({"first_name": "x"})})),
    ):
        _raises(lambda raw=raw: verify_init_data(raw, TOKEN), f"{label} must not pass")


def test_no_token_means_nothing_verifies() -> None:
    """A server without the token must refuse, never fall through to trusting."""
    _raises(lambda: verify_init_data(sign(good_fields()), ""), "an empty token must not pass")


def test_a_non_ascii_hash_is_a_flat_refusal() -> None:
    """compare_digest raises TypeError on non-ASCII, and that is not AuthError.

    A garbage hash from the wire must land in the same flat 401 as every other
    bad credential; a TypeError would escape _authenticate and drop the
    connection with a printed traceback instead of an answer.
    """
    raw = urlencode({**good_fields(), "hash": "хеш-не-хеш"})
    _raises(lambda: verify_init_data(raw, TOKEN),
            "a non-ASCII hash must refuse like any other bad hash")


def test_the_error_never_says_which_check_failed() -> None:
    """One flat message: a probe should not learn whether the hash or the clock lost."""
    seen = set()
    for raw in ("", sign(good_fields(age=48 * 3600)), sign(good_fields(), token="other")):
        try:
            verify_init_data(raw, TOKEN)
        except AuthError as exc:
            seen.add(str(exc))
    _ok(seen == {"bad initData"}, f"error text leaks which check failed: {seen}")


# --------------------------------------------------------------------- state


def _seed_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                            lang TEXT, banned INTEGER);
        CREATE TABLE submit_jobs (id INTEGER PRIMARY KEY, user_id INTEGER, project_id TEXT,
                                  status TEXT, total INTEGER, done INTEGER, ok INTEGER,
                                  fail INTEGER, already INTEGER, bad INTEGER, chat_id INTEGER,
                                  message_id INTEGER, detail TEXT, created_at TEXT,
                                  started_at TEXT, finished_at TEXT);
        CREATE TABLE user_project_stats (user_id INTEGER, project_id TEXT, ok_count INTEGER,
                                         updated_at TEXT);
        """
    )
    conn.execute("INSERT INTO users VALUES (7, 'op', 'Op', 'ru', 0)")
    conn.execute("INSERT INTO users VALUES (8, 'other', 'Other', 'ru', 0)")
    conn.executemany(
        "INSERT INTO submit_jobs (id,user_id,project_id,status,total,done,ok,fail,already,bad,"
        "created_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, 7, "demoa", "running", 100, 40, 38, 1, 1, 0, "2026-08-16T20:00", None),
            (2, 7, "demob", "done", 50, 50, 50, 0, 0, 0, "2026-08-16T19:00",
             "2026-08-16T19:20"),
            (3, 8, "demoa", "running", 10, 2, 2, 0, 0, 0, "2026-08-16T20:05", None),
        ],
    )
    conn.executemany(
        "INSERT INTO user_project_stats VALUES (?,?,?,?)",
        [(7, "demoa", 38, "x"), (7, "demor", 12, "x"), (8, "demoa", 2, "x")],
    )
    conn.commit()
    conn.close()


def test_state_shows_only_the_asking_user() -> None:
    path = str(Path(tempfile.mkdtemp(prefix="webstate_")) / "t.db")
    _seed_db(path)
    names = {"demoa": "Demoa", "demob": "Demob"}
    s = webstate.build_state(path, 7, names)

    _ok([j["id"] for j in s["active"]] == [1], s["active"])
    _ok([j["id"] for j in s["recent"]] == [2], s["recent"])
    _ok(all(t["project_id"] != "demoa" or t["ok_count"] == 38 for t in s["totals"]), s)
    # Job 3 belongs to user 8 and must be invisible here.
    everything = json.dumps(s)
    _ok('"id": 3' not in everything and "Other" not in everything, "another user leaked in")

    job = s["active"][0]
    _ok(job["project"] == "Demoa", "a raw project id must not reach the screen")
    _ok(job["percent"] == 40, job)
    _ok(s["summary"] == {"active_jobs": 1, "rows_in_flight": 60, "ok_total": 50}, s["summary"])
    # A project switched off still owns the rows it collected, and says so.
    off = [t for t in s["totals"] if t["project_id"] == "demor"]
    _ok(off and off[0]["live"] is False, s["totals"])


def test_state_cannot_write() -> None:
    """Read-only is the promise: a prototype must not be able to touch a job."""
    path = str(Path(tempfile.mkdtemp(prefix="webro_")) / "t.db")
    _seed_db(path)
    # `_connect` is a context manager now — it closes the descriptor as well as
    # ending the transaction, which is what stopped the read path from leaking
    # a handle per poll. What is asserted here is unchanged: read-only.
    try:
        with webstate._connect(path) as conn:
            conn.execute("UPDATE submit_jobs SET ok = 999 WHERE id = 1")
        raise AssertionError("the web app opened the bot database writable")
    except sqlite3.OperationalError:
        pass
    check = sqlite3.connect(path)
    _ok(check.execute("SELECT ok FROM submit_jobs WHERE id=1").fetchone()[0] == 38)
    check.close()


def test_zero_total_job_does_not_divide_by_zero() -> None:
    path = str(Path(tempfile.mkdtemp(prefix="webz_")) / "t.db")
    _seed_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO submit_jobs (id,user_id,project_id,status,total,done,ok,fail,already,bad,"
        "created_at) VALUES (9,7,'demoa','queued',0,0,0,0,0,0,'2026-08-16T21:00')"
    )
    conn.commit()
    conn.close()
    s = webstate.build_state(path, 7, {"demoa": "Demoa"})
    _ok([j["percent"] for j in s["active"] if j["id"] == 9] == [0], s["active"])


# ---------------------------------------------------------------- rate limit


def test_the_window_refuses_past_the_limit_and_recovers() -> None:
    w = ratelimit.SlidingWindow(3, 10.0)
    _ok([w.allow("a", now=100.0) for _ in range(3)] == [True] * 3)
    _ok(w.allow("a", now=100.0) is False, "the fourth in the window must be refused")
    # A different caller is unaffected — the limit is per key, not global.
    _ok(w.allow("b", now=100.0) is True)
    # Once the window rolls past, the same caller is welcome again.
    _ok(w.allow("a", now=111.0) is True, "the window must actually slide")


def test_the_ip_limit_leaves_room_for_carrier_nat() -> None:
    """A per-IP rule must not be a rule against people sharing an address.

    A page polls every 4 s (~15 req/min). Mobile carriers put thousands of
    subscribers behind one CGNAT address, so an IP window sized for a handful
    of clients blocks real users. The first version allowed four; that was the
    bug. The limit that actually identifies a caller is the per-user one, which
    NAT cannot blur.
    """
    per_page_per_min = 60 / 4
    concurrent = ratelimit.IP_LIMIT / per_page_per_min
    _ok(concurrent >= 20, f"only {concurrent:.0f} clients fit behind one IP")
    # One person legitimately has more than one screen open — a phone and a
    # desktop, or the app reopened while the old view is still polling. The
    # window has to clear several of those, not just one.
    _ok(ratelimit.USER_LIMIT >= 5 * per_page_per_min,
        f"only {ratelimit.USER_LIMIT / per_page_per_min:.0f} screens fit per user")


def test_client_ip_prefers_what_cloudflare_reports() -> None:
    _ok(ratelimit.client_ip({"CF-Connecting-IP": "203.0.113.7"}) == "203.0.113.7")
    # XFF is honored only from the loopback tunnel; on any other bind it is
    # client-controlled and must not buy a fresh IP window.
    _ok(
        ratelimit.client_ip(
            {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}, peer="127.0.0.1"
        )
        == "203.0.113.9"
    )
    _ok(
        ratelimit.client_ip(
            {"X-Forwarded-For": "203.0.113.9"}, peer="198.51.100.5"
        )
        == "198.51.100.5",
        "XFF ignored from a non-loopback peer",
    )
    _ok(ratelimit.client_ip({}) == "unknown")
    # A hostile value cannot grow the key space without bound.
    _ok(len(ratelimit.client_ip({"CF-Connecting-IP": "x" * 500})) <= 64)


def test_the_ledger_does_not_grow_without_bound() -> None:
    w = ratelimit.SlidingWindow(1, 1.0)
    for i in range(ratelimit.MAX_KEYS + 500):
        w.allow(f"k{i}", now=1000.0 + i)
    _ok(len(w._hits) <= ratelimit.MAX_KEYS, f"ledger grew to {len(w._hits)}")


def main() -> None:
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
