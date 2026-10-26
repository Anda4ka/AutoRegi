"""Paid plans: admin grant/revoke commands and the plan:/pay: callbacks.

Moved verbatim out of create_bot — same decorators, same indentation, bound
to the bot instance passed to register().
"""

from __future__ import annotations

import telebot


def register(bot: telebot.TeleBot, tg) -> None:
    import uuid
    from functools import partial

    from bot import billing
    from bot import billing_ux
    from bot import db as bot_db
    from bot import errors as bot_errors
    from bot import payments_np
    from bot import payments_poll
    from bot import rate_limit as bot_rate
    from bot import screen
    from telebot import types

    from bot.i18n import t
    from bot.ux import esc
    import handlers.common as _common

    _is_admin = _common.is_admin
    _admin_notify = partial(_common.admin_notify, bot)
    _deny_if_banned_call = partial(_common.deny_if_banned_call, bot)
    _gate_sub = partial(_common.gate_sub, bot)
    _screen = partial(_common.show_screen, bot)
    _screen_gate = partial(_common.screen_gate, bot)

    @bot.message_handler(commands=["grant_plan"])
    def cmd_grant_plan(message: types.Message) -> None:
        """/grant_plan <uid|@user> <basic|pro> [months] — manual grant."""
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        parts = (message.text or "").split()
        if len(parts) < 3:
            bot.reply_to(
                message,
                "Использование: <code>/grant_plan USER_ID basic|pro [месяцев]</code>",
            )
            return
        uid = bot_db.find_user_id(parts[1])
        if uid is None:
            bot.reply_to(message, "Пользователь не найден.")
            return
        plan_id = parts[2].strip().lower()
        months = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        ok, detail = billing.grant_plan(
            uid, plan_id, months=months, source="admin", admin_id=message.from_user.id
        )
        if not ok:
            bot.reply_to(message, f"❌ {esc(detail)}")
            return
        bot.reply_to(
            message,
            f"✅ <b>{esc(plan_id)}</b> для <code>{uid}</code> до "
            f"<b>{esc(detail[:10])}</b> · лимит {billing.plan_ok_limit(plan_id)}",
        )
        try:
            lang = bot_db.get_lang(uid)
            plan = billing.get_plan(plan_id)
            _admin_notify(
                uid,
                t(
                    "plan.granted",
                    lang,
                    emoji=plan.emoji,
                    name=plan.id.capitalize(),
                    until=billing_ux._fmt_date(detail, lang),
                ),
            )
        except Exception as exc:
            print(f"[admin] grant notice failed: {exc}", flush=True)

    @bot.message_handler(commands=["revoke_plan"])
    def cmd_revoke_plan(message: types.Message) -> None:
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            bot.reply_to(message, "Использование: <code>/revoke_plan USER_ID</code>")
            return
        uid = bot_db.find_user_id(parts[1])
        if uid is None:
            bot.reply_to(message, "Пользователь не найден.")
            return
        removed = billing.revoke_plan(uid, admin_id=message.from_user.id)
        bot.reply_to(
            message,
            f"✅ Тариф снят с <code>{uid}</code>."
            if removed
            else f"У <code>{uid}</code> нет активного тарифа.",
        )

    @bot.message_handler(commands=["subs", "subs_stats"])
    def cmd_subs(message: types.Message) -> None:
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        bot.reply_to(
            message,
            billing_ux.format_subscriptions_panel(),
            disable_web_page_preview=True,
        )

    @bot.message_handler(commands=["payment"])
    def cmd_payment(message: types.Message) -> None:
        """/payment <payment_id> — raw record for support (admin only)."""
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            bot.reply_to(message, "Использование: <code>/payment PAYMENT_ID</code>")
            return
        row = bot_db.get_payment_record(parts[1].strip())
        if row is None:
            bot.reply_to(message, "Платёж не найден.")
            return
        body = "\n".join(f"{k}: {esc(str(v))}" for k, v in row.items())
        bot.reply_to(message, f"<code>{body}</code>", disable_web_page_preview=True)

    @bot.callback_query_handler(
        func=lambda c: (c.data or "").startswith(("plans:", "plan:buy:", "pay:"))
    )
    def on_billing_cb(call: types.CallbackQuery) -> None:
        """Plans list, payment creation, manual status check, cancel."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        lang = bot_db.get_lang(u.id)
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        # The subsystem switch, not the shop switch: a stale button must still
        # reach the plans screen to be told sales are closed.
        if not billing_ux.payments_available(u.id):
            screen.toast(bot, call, t("pay.disabled", lang), alert=True)
            return

        data = call.data or ""

        if data == "plans:menu":
            _screen(u.id, call.message.chat.id, screen.STATE_PLANS, lang=lang, call=call)
            return

        if data.startswith("plan:buy:"):
            # New Basic invoices are gone. An already-open address is still
            # shown so a transfer in flight can settle.
            existing = bot_db.get_open_payment_for_user(u.id)
            if existing is not None:
                _screen(
                    u.id,
                    call.message.chat.id,
                    screen.STATE_PAY,
                    {"payment_id": str(existing["payment_id"])},
                    lang=lang,
                    call=call,
                )
                return
            _screen(
                u.id, call.message.chat.id, screen.STATE_PRIVATKA, lang=lang, call=call
            )
            return

        if data.startswith("pay:check:"):
            payment_id = data.split(":", 2)[-1].strip()
            record = bot_db.get_payment_record(payment_id)
            if record is None or int(record["user_id"]) != u.id:
                screen.toast(bot, call, t("toast.project_unavailable", lang), alert=True)
                return
            ok_rate, rate_msg = bot_rate.allow(u.id, "pay_check", lang=lang)
            if not ok_rate:
                screen.toast(bot, call, rate_msg, alert=True)
                return
            try:
                fresh = payments_np.get_payment(payment_id)
            except payments_np.NowPaymentsError as exc:
                screen.toast(
                    bot, call, bot_errors.user_message("FAIL", str(exc), lang=lang), alert=True
                )
                return
            outcome = payments_poll.handle_status(
                record, fresh, bot=bot, send=_admin_notify
            )
            screen.toast(
                bot,
                call,
                t("pay.status_done", lang)
                if outcome in ("credited", "already_credited")
                else t("pay.not_yet", lang),
            )
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_PAY,
                {"payment_id": payment_id},
                lang=lang,
            )
            return

        if data.startswith("pay:cancel:"):
            payment_id = data.split(":", 2)[-1].strip()
            record = bot_db.get_payment_record(payment_id)
            if record is not None and int(record["user_id"]) == u.id and not record["credited_at"]:
                # Local only: NOWPayments keeps the address alive, and a late
                # transfer is still credited by the poller.
                bot_db.update_payment_status(payment_id, "expired", mark_polled=False)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_PLANS,
                lang=lang,
                call=call,
                toast=t("pay.cancelled", lang),
            )
            return
