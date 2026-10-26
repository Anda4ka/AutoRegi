"""What a project still has left to do for this user, and what it costs to ask.

The screen used to answer "what will Start send" with the number of wallets
capped by free slots — the count *before* dedup. Measured on the operator's own
account on 2026-08-17, 18 of 21 projects had all 695 wallets already
registered, so eighteen Start buttons were offered and every one of them would
have refused. These tests pin the honest number, and the cache that makes
asking for it affordable.

The cache is keyed on facts, not on a clock: the accounts file's identity and a
per-project generation that moves whenever a settled row lands. There is no
staleness window to reason about — an entry either describes the world as it is
or it is thrown away.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fresh_"))
os.environ["BOT_TOKEN"] = "t:t"
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["USER_DATA_DIR"] = str(_TMP / "data")

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot."):
        del sys.modules[_mod]

from bot import db as bot_db  # noqa: E402
from bot import fresh as bot_fresh  # noqa: E402
from bot import user_data as user_data_mod  # noqa: E402

UID = 4242
WALLETS = ["0x" + f"{i:040x}" for i in range(1, 11)]


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _write_data(wallets: list[str]) -> None:
    path = user_data_mod.user_data_path(UID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"user{i}|{w}" for i, w in enumerate(wallets)) + "\n",
        encoding="utf-8",
    )
    user_data_mod.invalidate_rows_cache(UID)
    # Two writes inside one filesystem timestamp tick would look identical to a
    # stat-keyed cache; the tests below rewrite the file deliberately, so make
    # the stamp move.
    time.sleep(0.01)


def _setup() -> None:
    bot_db.init_db()
    # `submissions.user_id` is a foreign key into `users`; the bot always calls
    # `ensure_user` first, so a test that skips it fails on the constraint
    # rather than on the thing it means to check.
    bot_db.ensure_user(UID, "op", "Op")
    bot_db.ensure_user(9999, "other", "Other")
    with bot_db.db() as conn:
        conn.execute("DELETE FROM submissions")
        conn.commit()
    bot_fresh.invalidate()
    _write_data(WALLETS)


def test_an_untouched_project_has_every_wallet_free() -> None:
    _setup()
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10, "all ten are unseen")


def test_registered_wallets_stop_counting() -> None:
    _setup()
    for w in WALLETS[:4]:
        bot_db.record_submission(UID, "alpha", "h", w, "OK", "")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 6, bot_fresh.fresh_count(UID, "alpha"))
    # Another project has not seen any of them.
    _ok(bot_fresh.fresh_count(UID, "beta") == 10, "projects are counted apart")


def test_already_counts_the_same_as_ok() -> None:
    """ALREADY means the row is on their side — it is not work left to do."""
    _setup()
    bot_db.record_submission(UID, "alpha", "h", WALLETS[0], "ALREADY", "")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 9)


def test_a_failure_leaves_the_wallet_free() -> None:
    """A technical failure is retryable; counting it as done would hide work."""
    _setup()
    bot_db.record_submission(UID, "alpha", "h", WALLETS[0], "FAIL", "timeout")
    bot_db.record_submission(UID, "alpha", "h", WALLETS[1], "BAD_RESP", "nope")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10, bot_fresh.fresh_count(UID, "alpha"))


def test_someone_elses_registration_also_takes_the_wallet() -> None:
    """Dedup is global: the project will refuse it whoever sent it."""
    _setup()
    bot_db.record_submission(9999, "alpha", "h", WALLETS[0], "OK", "")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 9, bot_fresh.fresh_count(UID, "alpha"))


def test_a_new_row_invalidates_the_cached_answer() -> None:
    """The cache must not outlive the fact it describes."""
    _setup()
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10)
    bot_db.record_submission(UID, "alpha", "h", WALLETS[0], "OK", "")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 9, "a settled row must move the answer")


def test_a_row_on_one_project_does_not_disturb_another() -> None:
    """The counter is per project on purpose.

    A global one would be bumped by every row of a long run, so every other
    project's answer would be recomputed on every poll — the cache would be
    emptiest exactly when the machine is busiest.
    """
    _setup()
    _ok(bot_fresh.fresh_count(UID, "beta") == 10)
    before = bot_db.project_generation("beta")
    for w in WALLETS[:5]:
        bot_db.record_submission(UID, "alpha", "h", w, "OK", "")
    _ok(bot_db.project_generation("beta") == before, "beta's generation moved")
    _ok(bot_db.project_generation("alpha") > 0, "alpha's did not")


def test_a_new_accounts_file_is_a_new_answer() -> None:
    _setup()
    bot_db.record_submission(UID, "alpha", "h", WALLETS[0], "OK", "")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 9)
    # Replace the list with wallets the project has never seen.
    _write_data(["0x" + f"{i:040x}" for i in range(100, 103)])
    _ok(bot_fresh.fresh_count(UID, "alpha") == 3, bot_fresh.fresh_count(UID, "alpha"))


def test_no_accounts_means_nothing_to_do() -> None:
    _setup()
    user_data_mod.clear_user_data(UID)
    bot_fresh.invalidate(UID)
    _ok(bot_fresh.fresh_count(UID, "alpha") == 0)


def test_the_cache_actually_saves_the_query() -> None:
    """Not a micro-optimisation: this is 7 ms per project, 21 projects a poll."""
    _setup()
    calls = {"n": 0}
    real = bot_db.wallets_already_registered

    def counted(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    bot_db.wallets_already_registered = counted
    try:
        bot_fresh.invalidate()
        for _ in range(5):
            bot_fresh.fresh_count(UID, "alpha")
        _ok(calls["n"] == 1, f"asked the database {calls['n']} times for one answer")
    finally:
        bot_db.wallets_already_registered = real


def _count_queries():
    """Wrap the dedup query so a test can see how often it actually runs."""
    calls = {"n": 0}
    real = bot_db.wallets_already_registered

    def counted(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    bot_db.wallets_already_registered = counted
    return calls, real


def test_someone_elses_wallet_does_not_cost_me_a_query() -> None:
    """The generation moves for everyone; the answer changes for one person.

    Dedup is global, so a row settling anywhere bumps the project's counter and
    every cached answer for that project used to be discarded. While the queue
    is running that happens every few seconds — so the cache missed hardest
    exactly when the bot had least to spare, and each miss is a query per
    project on a four-second poll.
    """
    _setup()
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10)

    calls, real = _count_queries()
    try:
        # Somebody else registers wallets this user does not have.
        for i in range(3):
            bot_db.record_submission(
                9999, "alpha", "h", "0x" + f"{9000 + i:040x}", "OK", "")
        _ok(bot_fresh.fresh_count(UID, "alpha") == 10,
            "a stranger's wallet changed this user's count")
        _ok(calls["n"] == 0,
            f"someone else's row cost {calls['n']} queries")
    finally:
        bot_db.wallets_already_registered = real


def test_my_own_wallet_still_invalidates_immediately() -> None:
    """The saving must not become a stale answer for the person it concerns."""
    _setup()
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10)
    bot_db.record_submission(UID, "alpha", "h", WALLETS[0], "OK", "")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 9,
        "the user's own registration was not noticed")


def test_a_stranger_registering_my_wallet_is_still_noticed() -> None:
    """Global dedup: someone else's row on *my* wallet does take it away."""
    _setup()
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10)
    bot_db.record_submission(9999, "alpha", "h", WALLETS[1], "OK", "")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 9,
        "a wallet taken by someone else still counted as free")


def test_case_does_not_hide_a_settled_wallet() -> None:
    """Wallets arrive in whatever case the user typed them."""
    _setup()
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10)
    bot_db.record_submission(9999, "alpha", "h", WALLETS[2].upper(), "OK", "")
    _ok(bot_fresh.fresh_count(UID, "alpha") == 9,
        "an upper-case wallet slipped past the settled trail")


def test_falling_off_the_trail_recomputes_rather_than_guesses() -> None:
    """Past the trail's length the honest answer is "ask again", not "no change"."""
    _setup()
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10)
    # More settled rows than the trail holds, none of them this user's.
    over = bot_db._PROJECT_SETTLED_MAX + 10
    for i in range(over):
        bot_db.record_submission(
            9999, "alpha", "h", "0x" + f"{20000 + i:040x}", "OK", "")
    _ok(bot_db.project_settled_since("alpha", 1) is None,
        "the trail claimed to remember more than it holds")
    # And the count is still right, because it recomputed instead of trusting it.
    _ok(bot_fresh.fresh_count(UID, "alpha") == 10)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
