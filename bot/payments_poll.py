"""Background poller: watch open payments and credit the plan exactly once.

There is no IPN webhook — this host has no public HTTPS endpoint — so the bot
asks NOWPayments about its own open payments instead. Everything the loop
needs lives in the ``payments`` table, so a restart simply resumes.

Crediting goes through ``db.credit_payment_if_new`` and nowhere else: its
``credited_at IS NULL`` guard is what makes a repeated ``finished`` status, a
duplicated batch row or two threads land on one grant.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from bot import billing
from bot import billing_ux
from bot import config
from bot import db as bot_db
from bot import limits as bot_limits
from bot import payments_np
from bot import screen
from bot.i18n import t


BATCH = 25
# Spacing between API calls inside one cycle: <3 rps even with a full batch.
CALL_SPACING = 0.35


def _poll_interval() -> int:
    return max(5, int(getattr(config, "NOWPAYMENTS_POLL_INTERVAL", 20) or 20))


def _ttl_hours() -> int:
    return max(1, int(getattr(config, "NOWPAYMENTS_PAYMENT_TTL_HOURS", 24) or 24))


def _partial_ratio() -> Decimal:
    try:
        return Decimal(str(getattr(config, "PARTIAL_CREDIT_RATIO", 0.95)))
    except (InvalidOperation, ValueError):
        return Decimal("0.95")


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _age_seconds(payment: dict, now: datetime) -> float:
    created = bot_db.parse_iso(str(payment.get("created_at") or ""))
    if created is None:
        return 0.0
    return max(0.0, (now - created).total_seconds())


def _backoff_for(age_seconds: float) -> float:
    """How long to wait between checks — a payment nobody funded gets quieter."""
    if age_seconds < 600:
        return 20.0
    if age_seconds < 3600:
        return 60.0
    if age_seconds < 6 * 3600:
        return 300.0
    return 900.0


def _deadline(payment: dict) -> datetime | None:
    """When this payment stops being payable (NOWPayments window, else TTL)."""
    valid_until = bot_db.parse_iso(str(payment.get("valid_until") or ""))
    if valid_until is not None:
        return valid_until
    created = bot_db.parse_iso(str(payment.get("created_at") or ""))
    if created is None:
        return None
    return created + timedelta(hours=_ttl_hours())


def _is_due(payment: dict, now: datetime) -> bool:
    last = bot_db.parse_iso(str(payment.get("last_poll_at") or ""))
    if last is None:
        return True
    waited = (now - last).total_seconds()
    return waited >= _backoff_for(_age_seconds(payment, now))


def _lang(user_id: int) -> str:
    try:
        return bot_db.get_lang(user_id)
    except Exception:
        return "ru"


def refresh_payment_screen(bot, payment: dict, *, force_new: bool = False) -> None:
    """Redraw the payment on the user's live screen (or deliver a final card)."""
    if bot is None:
        return
    user_id = int(payment.get("user_id") or 0)
    chat_id = int(payment.get("chat_id") or 0) or user_id
    if not user_id:
        return
    lang = _lang(user_id)
    state, params = screen.current_state(user_id)
    on_this_payment = state == screen.STATE_PAY and str(
        params.get("payment_id") or ""
    ) == str(payment.get("payment_id") or "")
    if not on_this_payment and not force_new:
        return
    screen.show(
        bot,
        user_id,
        chat_id,
        screen.STATE_PAY,
        {"payment_id": str(payment.get("payment_id") or "")},
        lang=lang,
        force_new=force_new and not on_this_payment,
    )


def handle_status(
    payment: dict,
    np_payment,
    *,
    bot=None,
    send: Callable[[int, str], None] | None = None,
) -> str:
    """Apply one NOWPayments answer to our record. Returns what happened."""
    payment_id = str(payment.get("payment_id") or "")
    status = payments_np.normalize_status(np_payment.payment_status)
    paid = np_payment.actually_paid or ""
    user_id = int(payment.get("user_id") or 0)

    if status in payments_np.TERMINAL_OK:
        return _credit(payment, status, paid, bot=bot, send=send)

    if status in payments_np.PARTIAL:
        expected = _decimal(payment.get("pay_amount"))
        got = _decimal(paid)
        bot_db.update_payment_status(payment_id, status, actually_paid=paid)
        if expected > 0 and got >= expected * _partial_ratio():
            # Network fees can shave a little off; close enough counts as paid.
            return _credit(payment, status, paid, bot=bot, send=send)
        if send is not None:
            for admin_id in config.ADMIN_IDS:
                try:
                    send(
                        admin_id,
                        f"⚠️ Недоплата: payment <code>{payment_id}</code> "
                        f"user <code>{user_id}</code> — {paid}/{payment.get('pay_amount')} "
                        f"{payment.get('pay_currency')}",
                    )
                except Exception:
                    pass
        refresh_payment_screen(bot, bot_db.get_payment_record(payment_id) or payment)
        return "partial"

    if status in payments_np.TERMINAL_BAD:
        bot_db.update_payment_status(payment_id, status, actually_paid=paid)
        was_partial = _decimal(payment.get("actually_paid")) > 0
        if was_partial and send is not None:
            for admin_id in config.ADMIN_IDS:
                try:
                    send(
                        admin_id,
                        f"⚠️ Платёж <code>{payment_id}</code> (user <code>{user_id}</code>) "
                        f"закрыт как {status} с недоплатой {payment.get('actually_paid')}. "
                        f"Решение вручную: /grant_plan {user_id} {payment.get('plan')}",
                    )
                except Exception:
                    pass
        refresh_payment_screen(
            bot, bot_db.get_payment_record(payment_id) or payment, force_new=True
        )
        return status

    bot_db.update_payment_status(payment_id, status or "waiting", actually_paid=paid)
    refresh_payment_screen(bot, bot_db.get_payment_record(payment_id) or payment)
    return "waiting"


def _credit(
    payment: dict,
    status: str,
    paid: str,
    *,
    bot=None,
    send: Callable[[int, str], None] | None = None,
) -> str:
    payment_id = str(payment.get("payment_id") or "")
    user_id = int(payment.get("user_id") or 0)
    granted, detail = bot_db.credit_payment_if_new(payment_id)
    if not granted:
        # Restart, duplicate delivery or a parallel thread got here first.
        return "already_credited"
    bot_db.update_payment_status(payment_id, "finished", actually_paid=paid, mark_polled=False)
    bot_limits.invalidate(user_id)
    record = bot_db.get_payment_record(payment_id) or payment
    plan = billing.get_plan(str(record.get("plan") or ""))
    print(
        f"[pay] credited {payment_id} user={user_id} plan={plan.id} until={detail}",
        flush=True,
    )
    refresh_payment_screen(bot, record, force_new=True)
    if send is not None:
        for admin_id in config.ADMIN_IDS:
            try:
                send(
                    admin_id,
                    f"💰 Оплата: user <code>{user_id}</code> → <b>{plan.id}</b> "
                    f"(${record.get('price_amount')}) до {detail[:10]}",
                )
            except Exception:
                pass
    return "credited"


def poll_once(
    *,
    bot=None,
    send: Callable[[int, str], None] | None = None,
    now: datetime | None = None,
) -> dict:
    """One sweep over open payments. Returns counters for logs/tests."""
    now = now or datetime.now(timezone.utc)
    stats = {"checked": 0, "credited": 0, "expired": 0, "failed": 0, "skipped": 0}
    # No cutoff on purpose. With one, a payment older than the poll window
    # became immortal: never listed, so never polled and never marked expired,
    # while get_open_payment_for_user kept handing the dead address to
    # plan:buy forever. Rows past their window are closed locally below (no
    # API call), and any row older than the cutoff always is: its deadline is
    # at most created_at + TTL, which the cutoff (8x TTL) outlives.
    for payment in bot_db.list_pollable_payments(cutoff_iso="", limit=BATCH):
        payment_id = str(payment.get("payment_id") or "")
        deadline = _deadline(payment)
        if deadline is not None and now > deadline:
            # Past the payable window — but a `finished` verdict can land
            # after it (provider/chain race), and money already sent must not
            # vanish silently. Ask once more before closing locally.
            np_payment = None
            try:
                np_payment = payments_np.get_payment(payment_id)
            except payments_np.NowPaymentsError as exc:
                print(f"[pay] final poll {payment_id}: {exc}", flush=True)
            if np_payment is not None:
                stats["checked"] += 1
                outcome = handle_status(payment, np_payment, bot=bot, send=send)
                if outcome in ("credited", "already_credited"):
                    stats["credited"] += 1
                    continue
                if outcome == "partial":
                    # Admins were alerted by handle_status; close so the next
                    # sweep does not repeat the final poll forever.
                    bot_db.update_payment_status(payment_id, "expired")
                    stats["expired"] += 1
                    continue
                if outcome != "waiting":
                    # Terminal bad: the row is already closed and alerted.
                    stats["expired"] += 1
                    continue
                payment = bot_db.get_payment_record(payment_id) or payment
            # Close locally, no further API calls; an underpayment is a
            # human decision, so the operator hears about it once.
            bot_db.update_payment_status(payment_id, "expired")
            if _decimal(payment.get("actually_paid")) > 0 and send is not None:
                user_id = int(payment.get("user_id") or 0)
                for admin_id in config.ADMIN_IDS:
                    try:
                        send(
                            admin_id,
                            f"⚠️ Платёж <code>{payment_id}</code> "
                            f"(user <code>{user_id}</code>) истёк с недоплатой "
                            f"{payment.get('actually_paid')} "
                            f"{payment.get('pay_currency')}. "
                            f"Решение вручную: /grant_plan {user_id} "
                            f"{payment.get('plan')}",
                        )
                    except Exception:
                        pass
            refresh_payment_screen(
                bot, bot_db.get_payment_record(payment_id) or payment, force_new=True
            )
            stats["expired"] += 1
            continue
        if not _is_due(payment, now):
            stats["skipped"] += 1
            continue
        try:
            np_payment = payments_np.get_payment(payment_id)
        except payments_np.NowPaymentsError as exc:
            print(f"[pay] poll {payment_id}: {exc}", flush=True)
            bot_db.update_payment_status(
                payment_id, str(payment.get("status") or "waiting"), detail=str(exc)
            )
            stats["failed"] += 1
            time.sleep(CALL_SPACING)
            continue
        stats["checked"] += 1
        outcome = handle_status(payment, np_payment, bot=bot, send=send)
        if outcome == "credited":
            stats["credited"] += 1
        elif outcome in payments_np.TERMINAL_BAD:
            stats["expired" if outcome == "expired" else "failed"] += 1
        time.sleep(CALL_SPACING)
    return stats


def start_payment_poller(*, bot, send: Callable[[int, str], None]) -> threading.Thread:
    """Daemon loop; every failure is logged and swallowed (same as alerts)."""

    def _loop() -> None:
        print(
            f"[pay] poller started (every {_poll_interval()}s, {payments_np.api_base()})",
            flush=True,
        )
        while True:
            try:
                stats = poll_once(bot=bot, send=send)
                if stats["credited"] or stats["expired"] or stats["failed"]:
                    print(f"[pay] {stats}", flush=True)
            except Exception as exc:  # pragma: no cover - loop must never die
                print(f"[pay] poller error: {exc}", flush=True)
            time.sleep(_poll_interval())

    thread = threading.Thread(target=_loop, name="payments-poller", daemon=True)
    thread.start()
    return thread


def maybe_subscription_reminders(
    *,
    send: Callable[[int, str], None],
    now_utc: datetime | None = None,
) -> int:
    """Renewal nudges at SUB_REMIND_DAYS and once on expiry. Deduped per expiry."""
    now = now_utc or datetime.now(timezone.utc)
    sent = 0
    buckets = tuple(getattr(config, "SUB_REMIND_DAYS", (3, 1)) or (3, 1))
    for days in buckets:
        start = (now + timedelta(days=days - 1)).isoformat()
        end = (now + timedelta(days=days)).isoformat()
        for row in bot_db.subscriptions_expiring_between(start, end):
            uid = int(row["user_id"])
            marker = f"sub_remind:{uid}:{row['expires_at']}:{days}"
            if not bot_db.get_alert_marker(marker):
                plan = billing.get_plan(str(row["plan"]))
                lang = _lang(uid)
                try:
                    send(
                        uid,
                        t(
                            "plan.expiring"
                            if billing.sales_open()
                            else "plan.expiring_closed",
                            lang,
                            emoji=plan.emoji,
                            name=plan.id.capitalize(),
                            days=days,
                            until=billing_ux._fmt_date(str(row["expires_at"]), lang),
                        ),
                    )
                    bot_db.set_alert_marker(marker, str(days))
                    sent += 1
                except Exception:
                    pass
    # And once after it lapses.
    for row in bot_db.subscriptions_expiring_between(
        (now - timedelta(days=1)).isoformat(), now.isoformat()
    ):
        uid = int(row["user_id"])
        marker = f"sub_expired:{uid}:{row['expires_at']}"
        if bot_db.get_alert_marker(marker):
            continue
        plan = billing.get_plan(str(row["plan"]))
        lang = _lang(uid)
        try:
            send(
                uid,
                t(
                    "plan.expired" if billing.sales_open() else "plan.expired_closed",
                    lang,
                    name=plan.id.capitalize(),
                    n=bot_db.get_free_limit(),
                ),
            )
            bot_db.set_alert_marker(marker, "1")
            sent += 1
        except Exception:
            pass
    return sent
