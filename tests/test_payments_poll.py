"""Payment poller: idempotent crediting, partial payments, expiry, restart."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="np_poll_test_"))
_DB = _TMP / "t.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["NOWPAYMENTS_API_KEY"] = "test-key"
os.environ["NOWPAYMENTS_SANDBOX"] = "1"
os.environ["LIMITS_CACHE_TTL"] = "0"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import billing  # noqa: E402
from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import limits as bot_limits  # noqa: E402
from bot import payments_np as np  # noqa: E402
from bot import payments_poll as poll  # noqa: E402
import bot.db as dbmod  # noqa: E402

config.DB_PATH = _DB
dbmod.DB_PATH = _DB
bot_db.init_db()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _np(status: str, *, paid: str = "", amount: str = "2.0") -> np.NPPayment:
    return np.NPPayment(
        payment_id="p",
        order_id="o",
        pay_address="0x" + "a" * 40,
        pay_amount=amount,
        pay_currency="usdtbsc",
        price_amount="2",
        payment_status=status,
        actually_paid=paid,
    )


def _make(payment_id: str, uid: int, plan: str = "basic", *, amount: str = "2.0",
          valid_until: str = "") -> dict:
    bot_db.ensure_user(uid, "u", "U")
    bot_db.create_payment_record(
        payment_id=payment_id,
        order_id=f"order-{payment_id}",
        user_id=uid,
        plan=plan,
        price_amount="2" if plan == "basic" else "10",
        pay_address="0x" + "a" * 40,
        pay_amount=amount,
        valid_until=valid_until or _iso(days=7),
    )
    return bot_db.get_payment_record(payment_id)


class Sink:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def __call__(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def test_finished_credits_once() -> None:
    uid = 30001
    record = _make("pay-1", uid)
    sink = Sink()
    out = poll.handle_status(record, _np("finished"), send=sink)
    _assert(out == "credited", out)
    _assert(bot_limits.effective_ok_limit(uid) == 100, "basic active")

    again = poll.handle_status(
        bot_db.get_payment_record("pay-1"), _np("finished"), send=sink
    )
    _assert(again == "already_credited", again)
    _assert(billing.days_left(uid) == 30, "a repeat must not add days")


def test_concurrent_finished_credits_once() -> None:
    uid = 30002
    record = _make("pay-race", uid, "basic")
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        out = poll.handle_status(dict(record), _np("finished"))
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    _assert(results.count("credited") == 1, f"exactly one credit: {results}")
    _assert(billing.days_left(uid) == 30, "no double extension")


def test_partial_below_threshold_does_not_credit() -> None:
    uid = 30003
    record = _make("pay-short", uid, amount="10.0")
    sink = Sink()
    out = poll.handle_status(record, _np("partially_paid", paid="8.0", amount="10.0"), send=sink)
    _assert(out == "partial", out)
    _assert(billing.active_plan(uid) is None, "underpaid must not grant a plan")
    row = bot_db.get_payment_record("pay-short")
    _assert(row["actually_paid"] == "8.0", "what arrived is recorded")
    _assert(row["credited_at"] is None, "not credited")


def test_partial_within_tolerance_credits() -> None:
    uid = 30004
    record = _make("pay-almost", uid, amount="10.0")
    # 9.6 of 10.0 = 96% — network fees, still counts.
    out = poll.handle_status(record, _np("partially_paid", paid="9.6", amount="10.0"))
    _assert(out == "credited", out)
    _assert(billing.is_active(uid), "plan granted on a near-full payment")


def test_partial_then_finished_credits_once() -> None:
    uid = 30005
    record = _make("pay-two-step", uid, amount="10.0")
    poll.handle_status(record, _np("partially_paid", paid="1.0", amount="10.0"))
    _assert(billing.active_plan(uid) is None, "still unpaid")
    out = poll.handle_status(bot_db.get_payment_record("pay-two-step"), _np("finished"))
    _assert(out == "credited", out)
    again = poll.handle_status(bot_db.get_payment_record("pay-two-step"), _np("finished"))
    _assert(again == "already_credited", "topping up does not double-credit")


def test_terminal_failure_is_recorded() -> None:
    uid = 30006
    record = _make("pay-dead", uid)
    out = poll.handle_status(record, _np("failed"))
    _assert(out == "failed", out)
    row = bot_db.get_payment_record("pay-dead")
    _assert(row["status"] == "failed", row["status"])
    _assert(billing.active_plan(uid) is None, "no plan from a failed payment")
    _assert(bot_db.get_open_payment_for_user(uid) is None, "not offered again")


def test_backoff_grows_with_age() -> None:
    _assert(poll._backoff_for(60) == 20.0, "fresh payments are checked often")
    _assert(poll._backoff_for(1800) == 60.0, "after 10 min")
    _assert(poll._backoff_for(3 * 3600) == 300.0, "after an hour")
    _assert(poll._backoff_for(20 * 3600) == 900.0, "old payments idle")


def test_due_respects_last_poll() -> None:
    now = datetime.now(timezone.utc)
    never = {"created_at": now.isoformat(), "last_poll_at": ""}
    _assert(poll._is_due(never, now), "never polled → due")
    just = {"created_at": now.isoformat(), "last_poll_at": now.isoformat()}
    _assert(not poll._is_due(just, now), "just polled → wait")
    stale = {
        "created_at": now.isoformat(),
        "last_poll_at": (now - timedelta(seconds=25)).isoformat(),
    }
    _assert(poll._is_due(stale, now), "past the backoff → due again")


def test_deadline_prefers_the_provider_window() -> None:
    # NOWPayments gave us a 7-day window; the local TTL must not cut it short.
    record = _make("pay-window", 30007, valid_until=_iso(days=7))
    deadline = poll._deadline(record)
    _assert(deadline is not None and deadline > datetime.now(timezone.utc) + timedelta(days=6),
            f"provider window kept: {deadline}")

    no_window = dict(record)
    no_window["valid_until"] = ""
    fallback = poll._deadline(no_window)
    _assert(fallback is not None, "falls back to created_at + TTL")
    _assert(
        fallback < datetime.now(timezone.utc) + timedelta(hours=25),
        f"fallback is the short TTL: {fallback}",
    )


def test_poll_expires_past_window_with_one_final_check() -> None:
    uid = 30008
    _make("pay-old", uid, valid_until=_iso(days=-1))
    calls: list[str] = []

    def _record(payment_id):
        calls.append(payment_id)
        return _np("waiting")

    original, np.get_payment = np.get_payment, _record
    try:
        stats = poll.poll_once()
    finally:
        np.get_payment = original
    _assert(stats["expired"] >= 1, stats)
    _assert(calls.count("pay-old") == 1,
            f"exactly one final check past the window: {calls}")
    _assert(bot_db.get_payment_record("pay-old")["status"] == "expired", "closed locally")
    # And closed means closed: the next sweep does not re-ask.
    calls.clear()
    original, np.get_payment = np.get_payment, _record
    try:
        poll.poll_once()
    finally:
        np.get_payment = original
    _assert("pay-old" not in calls, f"expired rows are not polled again: {calls}")


def test_expired_underpayment_alerts_admins() -> None:
    """A partial payment that outlives its window must not vanish silently."""
    # Hermetic precondition: the alert loop iterates config.ADMIN_IDS, and a
    # machine without a .env (CI) has none — the test must bring its own admin.
    prev_admins = set(config.ADMIN_IDS)
    config.ADMIN_IDS = {999001}
    try:
        _alert_run()
    finally:
        config.ADMIN_IDS = prev_admins


def _alert_run() -> None:
    uid = 30022
    _make("pay-late", uid, amount="10.0", valid_until=_iso(days=-1))
    with dbmod.db() as conn:
        conn.execute(
            "UPDATE payments SET actually_paid = '8.0' WHERE payment_id = 'pay-late'"
        )
        conn.commit()
    original = np.get_payment
    np.get_payment = lambda pid: _np("waiting", paid="8.0", amount="10.0")
    sink = Sink()
    try:
        stats = poll.poll_once(send=sink)
    finally:
        np.get_payment = original
    _assert(stats["expired"] >= 1, stats)
    row = bot_db.get_payment_record("pay-late")
    _assert(row["status"] == "expired", row["status"])
    _assert(any("pay-late" in text for _, text in sink.messages),
            f"admin was told about the underpayment: {sink.messages}")
    _assert(any("/grant_plan" in text for _, text in sink.messages),
            "the alert names the manual resolution command")


def test_ancient_open_payment_is_not_immortal() -> None:
    """Older than the poll window (8x TTL) must not mean "never close".

    The sweep used to list only recent payments, so a `waiting` row from a
    week-long downtime was never polled and never marked expired — while
    get_open_payment_for_user kept returning it and plan:buy re-showed the
    dead address forever, blocking the user's only open-payment slot.
    """
    uid = 30021
    # No provider window: the local deadline is created_at + TTL (24 h), both
    # long past for a payment created 9 days ago.
    row = _make("pay-ancient", uid)
    with dbmod.db() as conn:
        conn.execute(
            "UPDATE payments SET valid_until = '', created_at = ? "
            "WHERE payment_id = ?",
            (_iso(days=-9), "pay-ancient"),
        )
        conn.commit()
    cutoff = _iso(days=-192 // 24)
    _assert(
        all(p["payment_id"] != "pay-ancient"
            for p in bot_db.list_pollable_payments(cutoff_iso=cutoff)),
        "precondition: the old poll window would have hidden this payment",
    )

    # The deadline path now makes one final live check before closing.
    original, np.get_payment = np.get_payment, (lambda pid: _np("waiting"))
    try:
        stats = poll.poll_once()
    finally:
        np.get_payment = original
    _assert(stats["expired"] >= 1, stats)
    record = bot_db.get_payment_record("pay-ancient")
    _assert(record["status"] == "expired",
            f"ancient payment must close locally: {record['status']}")
    _assert(bot_db.get_open_payment_for_user(uid) is None,
            "the dead address must stop blocking new payments")


def test_poll_reads_open_payments_and_credits() -> None:
    uid = 30009
    _make("pay-live", uid)
    original = np.get_payment
    np.get_payment = lambda pid: _np("finished")
    try:
        stats = poll.poll_once()
    finally:
        np.get_payment = original
    _assert(stats["credited"] >= 1, stats)
    _assert(billing.is_active(uid), "plan is live after the sweep")
    # A credited payment leaves the working set.
    _assert(
        all(p["payment_id"] != "pay-live" for p in bot_db.list_pollable_payments(cutoff_iso=_iso(days=-30))),
        "credited payments stop being polled",
    )


def test_poll_survives_api_errors() -> None:
    uid = 30010
    _make("pay-flaky", uid)
    original = np.get_payment

    def _fail(pid):
        raise np.NowPaymentsError("gateway down", code="network", retryable=True)

    np.get_payment = _fail
    try:
        stats = poll.poll_once()
    finally:
        np.get_payment = original
    _assert(stats["failed"] >= 1, stats)
    row = bot_db.get_payment_record("pay-flaky")
    _assert(row["status"] == "waiting", "payment stays open for the next sweep")
    _assert(row["credited_at"] is None, "an API error never grants a plan")


def test_poller_resumes_after_restart() -> None:
    uid = 30011
    _make("pay-restart", uid)
    # Simulate a restart: nothing in memory, everything from SQLite.
    pending = bot_db.list_pollable_payments(cutoff_iso=_iso(days=-30))
    _assert(any(p["payment_id"] == "pay-restart" for p in pending), "picked up from the DB")
    row = next(p for p in pending if p["payment_id"] == "pay-restart")
    _assert(row["pay_address"].startswith("0x"), "address survived")
    _assert(row["pay_amount"], "amount survived")


def test_reminders_fire_once_per_expiry() -> None:
    uid = 30012
    bot_db.ensure_user(uid, "u", "U")
    bot_db.upsert_subscription(uid, "basic", _iso(days=1, hours=-1), source="admin")
    sink = Sink()
    sent = poll.maybe_subscription_reminders(send=sink)
    _assert(sent >= 1, f"a reminder went out: {sent}")
    again = poll.maybe_subscription_reminders(send=sink)
    _assert(again == 0, "and only once for this expiry date")

    # Renewal moves expires_at, which re-arms the reminder.
    bot_db.upsert_subscription(uid, "basic", _iso(days=1, minutes=-30), source="admin")
    _assert(poll.maybe_subscription_reminders(send=sink) >= 1, "new expiry, new reminder")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
