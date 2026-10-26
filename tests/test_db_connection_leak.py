"""Every `with db()` block must give its file descriptors back.

`bot.db.db` used to return the connection bare, and `sqlite3.Connection`'s
context manager ends the transaction without closing anything. What reclaimed
the descriptors was the cyclic collector, so the process ran with a backlog of
open handles that grew at about ten a minute against a soft RLIMIT_NOFILE of
1024. It exhausted twice on 2026-08-17 — once as `[Errno 24] Too many open
files` writing a queue payload, once as sqlite's `unable to open database
file`, which is the same exhaustion reported as if the database were gone.

The regression is invisible in behaviour: every query still returns the right
answer, and the leak only shows as an outage hours later under load. So the
test counts descriptors rather than results, and it must not call
`gc.collect()` — collecting is exactly what used to paper over the bug.
"""

from __future__ import annotations

import gc
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fdleak_test_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_RESULTS"] = str(_TMP / "r.jsonl")
os.environ["USER_DATA_DIR"] = str(_TMP / "ud")
os.environ["USER_PROXY_DIR"] = str(_TMP / "up")

_FD_DIR = Path("/proc/self/fd")


def _open_fds() -> int:
    """How many descriptors this process holds, or -1 where /proc is absent."""
    try:
        return len(os.listdir(_FD_DIR))
    except OSError:
        return -1


def test_db_blocks_do_not_leak_descriptors() -> None:
    from bot import db as bot_db

    bot_db.init_db()
    uid = 771001
    bot_db.ensure_user(uid, "fdleak", "FD")

    # Warm-up first: the very first calls import modules and open the results
    # file, and that one-off growth is not what is being measured.
    for _ in range(5):
        bot_db.get_ok_count(uid, "leaktest")

    before = _open_fds()
    if before < 0:
        return  # no /proc on this platform; nothing to assert against

    for _ in range(60):
        bot_db.get_ok_count(uid, "leaktest")
        bot_db.get_lang(uid)

    after = _open_fds()
    # No gc.collect() on purpose — see the module docstring. A couple of
    # descriptors of slack absorbs anything the results log or WAL does.
    assert after - before <= 2, (
        f"120 db() blocks leaked {after - before} descriptors "
        f"({before} -> {after}); bot.db.db must close its connection"
    )


def test_db_still_commits_and_rolls_back() -> None:
    """Closing must not cost the transaction semantics the call sites rely on."""
    import sqlite3

    from bot import db as bot_db

    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS leak_probe (k TEXT PRIMARY KEY, v TEXT)"
        )
        conn.execute("INSERT OR REPLACE INTO leak_probe VALUES ('a', '1')")

    with bot_db.db() as conn:
        assert conn.execute(
            "SELECT v FROM leak_probe WHERE k = 'a'"
        ).fetchone()[0] == "1", "clean exit must commit"

    try:
        with bot_db.db() as conn:
            conn.execute("INSERT OR REPLACE INTO leak_probe VALUES ('b', '2')")
            raise sqlite3.IntegrityError("forced")
    except sqlite3.IntegrityError:
        pass

    with bot_db.db() as conn:
        assert conn.execute(
            "SELECT v FROM leak_probe WHERE k = 'b'"
        ).fetchone() is None, "an exception must still roll the block back"


def test_webapp_state_connection_is_closed() -> None:
    """The mini app's read path runs every four seconds; it leaked too."""
    from bot import db as bot_db
    from bot import submit_queue as bot_queue
    from webapp.state import build_state

    bot_db.init_db()
    bot_queue.init_schema()  # build_state reads submit_jobs
    uid = 771002
    bot_db.ensure_user(uid, "fdleak2", "FD")

    for _ in range(3):
        build_state(str(bot_db.DB_PATH), uid, {})

    before = _open_fds()
    if before < 0:
        return

    for _ in range(40):
        build_state(str(bot_db.DB_PATH), uid, {})

    after = _open_fds()
    assert after - before <= 2, (
        f"40 build_state calls leaked {after - before} descriptors "
        f"({before} -> {after})"
    )


def test_leak_check_is_not_measuring_nothing() -> None:
    """Guard the guard: prove the counter would notice a connection kept open.

    Without this, a `_open_fds` that silently returned a constant would let
    both tests above pass while the leak was back.
    """
    import sqlite3

    before = _open_fds()
    if before < 0:
        return
    held = [sqlite3.connect(str(_TMP / "probe.db")) for _ in range(10)]
    assert _open_fds() - before >= 10, "descriptor counter is not measuring"
    for conn in held:
        conn.close()
    del held
    gc.collect()
