"""Text and keyboards for the paid plans, the payment screen and the admin panel.

Mirrors bot/admin_ux.py: pure builders, no Telegram calls, no HTTP.
"""

from __future__ import annotations

from datetime import datetime, timezone

from telebot import types

from bot import billing
from bot import config
from bot import db as bot_db
from bot import limits as bot_limits
from bot import payments_np
from bot.i18n import t
from bot.ux import esc


def payments_available(user_id: int) -> bool:
    """Paid UI is shown only when a key exists, the flag is on and, during the
    rollout, only to the allow-listed test ids.

    Canonical home of the payments visibility gates: both the Telegram shell
    and the screen builders call these through ``billing_ux.`` module access,
    so patching one place moves every reader at once.
    """
    if not payments_np.is_enabled() or not bool(getattr(config, "PAYMENTS_ENABLED", False)):
        return False
    allow = getattr(config, "PAYMENTS_TEST_USER_IDS", set())
    return not allow or int(user_id) in allow


def sales_open_for(user_id: int) -> bool:
    """May this user start a new payment right now?"""
    return payments_available(user_id) and billing.sales_open()


def billing_visible(user_id: int) -> bool:
    """Whether to show the plan line and the 💎 button at all.

    A closed shop still owes its holders a screen: someone who paid for 30 days
    must be able to see what they hold and when it ends.
    """
    return sales_open_for(user_id) or billing.is_active(user_id)


def _fmt_date(iso: str, lang: str = "ru") -> str:
    parsed = bot_db.parse_iso(iso)
    if parsed is None:
        return "—"
    return parsed.strftime("%d.%m.%Y" if lang == "ru" else "%Y-%m-%d")


def _fmt_price(value) -> str:
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def plan_line(plan: billing.Plan, lang: str) -> str:
    return t(
        "plan.card",
        lang,
        emoji=plan.emoji,
        name=plan.id.capitalize(),
        price=_fmt_price(plan.price_usd),
        days=plan.days,
        limit=plan.ok_limit,
    )


def current_plan_line(user_id: int, lang: str) -> str:
    """One line for the state card and the plans screen."""
    caps = bot_limits.tier_caps(user_id)
    current = billing.active_plan(user_id)
    if current is None:
        return t("plan.current_free", lang, n=caps.ok_limit)
    plan = billing.get_plan(current[0])
    return t(
        "plan.current_paid",
        lang,
        emoji=plan.emoji,
        name=plan.id.capitalize(),
        until=_fmt_date(current[1], lang),
        n=caps.ok_limit,
        days=billing.days_left(user_id),
    )


def plans_text(user_id: int, lang: str) -> str:
    lines = [t("plan.title", lang), "", current_plan_line(user_id, lang), ""]
    if not billing.sales_open():
        # No price list once the shop is shut: showing cards nobody can buy
        # reads as a bug. Holders still get their line and their end date.
        key = (
            "plan.sales_closed_paid"
            if billing.is_active(user_id)
            else "plan.sales_closed_free"
        )
        lines.append(t(key, lang))
        return "\n".join(lines).strip()
    for plan in billing.plan_catalog():
        lines.append(plan_line(plan, lang))
        if not billing.is_purchasable(plan.id):
            lines.append(t("plan.unavailable", lang))
        lines.append("")
    lines.append(t("plan.network_note", lang))
    return "\n".join(lines).strip()


def plans_keyboard(user_id: int, lang: str) -> types.InlineKeyboardMarkup:
    del user_id
    kb = types.InlineKeyboardMarkup()
    for plan in billing.plan_catalog():
        if not billing.is_sellable(plan.id):
            continue
        kb.add(
            types.InlineKeyboardButton(
                t(
                    "btn.buy_plan",
                    lang,
                    emoji=plan.emoji,
                    name=plan.id.capitalize(),
                    price=_fmt_price(plan.price_usd),
                ),
                callback_data=f"plan:buy:{plan.id}",
            )
        )
    kb.add(
        types.InlineKeyboardButton(t("btn.to_projects", lang), callback_data="projects:back")
    )
    return kb


def _countdown(valid_until: str, lang: str) -> str:
    parsed = bot_db.parse_iso(valid_until)
    if parsed is None:
        return ""
    seconds = (parsed - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return t("pay.expired_note", lang)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if hours >= 24:
        return t("pay.valid_days", lang, n=hours // 24)
    if hours:
        return t("pay.valid_hours", lang, h=hours, m=minutes)
    return t("pay.valid_minutes", lang, m=max(1, minutes))


def payment_text(payment: dict, lang: str) -> str:
    """Address, exact amount, network warning, countdown, current status."""
    plan = billing.get_plan(str(payment.get("plan") or ""))
    status = str(payment.get("status") or "waiting")
    lines = [
        t(
            "pay.title",
            lang,
            emoji=plan.emoji,
            name=plan.id.capitalize(),
            days=plan.days * max(1, int(payment.get("months") or 1)),
        ),
        "",
        t("pay.amount", lang, amount=esc(str(payment.get("pay_amount") or ""))),
        t("pay.network", lang),
        "",
        t("pay.address", lang),
        f"<code>{esc(str(payment.get('pay_address') or ''))}</code>",
        "",
    ]
    countdown = _countdown(str(payment.get("valid_until") or ""), lang)
    if countdown:
        lines.append(countdown)

    if status in ("confirming", "confirmed", "sending"):
        lines.append(t("pay.status_confirming", lang))
    elif status == "partially_paid":
        lines.append(
            t(
                "pay.status_partial",
                lang,
                paid=esc(str(payment.get("actually_paid") or "0")),
                amount=esc(str(payment.get("pay_amount") or "")),
            )
        )
    elif status == "finished":
        lines.append(t("pay.status_done", lang))
    elif status in ("failed", "refunded", "expired"):
        lines.append(t("pay.status_dead", lang))
    else:
        lines.append(t("pay.status_waiting", lang))
    return "\n".join(lines)


def payment_keyboard(payment: dict, lang: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    pid = str(payment.get("payment_id") or "")
    status = str(payment.get("status") or "waiting")
    if status in ("failed", "refunded", "expired"):
        plan = str(payment.get("plan") or billing.PLAN_BASIC)
        if billing.is_sellable(plan):
            kb.add(
                types.InlineKeyboardButton(
                    t("btn.pay_retry", lang), callback_data=f"plan:buy:{plan}"
                )
            )
    elif status != "finished":
        kb.add(
            types.InlineKeyboardButton(
                t("btn.pay_check", lang), callback_data=f"pay:check:{pid}"
            )
        )
        kb.add(
            types.InlineKeyboardButton(
                t("btn.pay_cancel", lang), callback_data=f"pay:cancel:{pid}"
            )
        )
    kb.add(types.InlineKeyboardButton(t("btn.plans", lang), callback_data="plans:menu"))
    return kb


def paid_success_text(user_id: int, payment: dict, lang: str) -> str:
    plan = billing.get_plan(str(payment.get("plan") or ""))
    current = billing.active_plan(user_id)
    until = _fmt_date(current[1], lang) if current else "—"
    return t(
        "pay.success",
        lang,
        emoji=plan.emoji,
        name=plan.id.capitalize(),
        until=until,
        n=billing.plan_ok_limit(plan.id),
    )


# --- admin ---


def format_subscriptions_panel(*, limit: int = 15) -> str:
    counts = bot_db.count_active_subscriptions()
    revenue = bot_db.revenue_stats(days=30)
    lines = [
        "<b>💎 Подписки</b>",
        f"активных: <b>{counts.get('total', 0)}</b>"
        + "".join(
            f" · {pid}: {n}" for pid, n in sorted(counts.items()) if pid != "total"
        ),
        f"выручка 30д: <b>${revenue['window']:.2f}</b> ({revenue['window_count']} платежей)",
        f"всего: <b>${revenue['total']:.2f}</b> ({revenue['count']} платежей)",
    ]
    by_plan = revenue.get("by_plan") or {}
    if by_plan:
        lines.append(
            "по тарифам: "
            + " · ".join(f"{pid} ${amount:.2f}" for pid, amount in sorted(by_plan.items()))
        )
    lines.append("")
    rows = bot_db.list_active_subscriptions(limit=limit)
    if not rows:
        lines.append("• нет активных подписок")
    for row in rows:
        lines.append(
            f"• <code>{row['user_id']}</code> {esc(str(row['plan']))} "
            f"до {_fmt_date(str(row['expires_at']))} ({esc(str(row.get('source') or '—'))})"
        )
    return "\n".join(lines)


def format_payments_panel(*, limit: int = 10) -> str:
    from bot import payments_np

    lines = ["<b>💳 NOWPayments</b>"]
    if not payments_np.is_enabled():
        lines.append("❌ NOWPAYMENTS_API_KEY не задан — платный раздел скрыт")
        return "\n".join(lines)
    lines.append(f"режим: <code>{esc(payments_np.api_base())}</code>")
    lines.append(f"валюта: <code>{esc(payments_np.pay_currency())}</code>")
    enabled = bool(getattr(config, "PAYMENTS_ENABLED", False))
    allow = getattr(config, "PAYMENTS_TEST_USER_IDS", set())
    lines.append(
        f"платежи: <b>{'включены' if enabled else 'выключены'}</b>"
        + (f" · тест-доступ: {len(allow)} id" if allow else "")
    )
    lines.append(
        "продажа: <b>открыта</b>"
        if billing.sales_open()
        else "продажа: <b>закрыта</b> (SALES_ENABLED=0) — действующие подписки живут до конца срока"
    )
    minimum = billing.min_amount_usd()
    lines.append(
        f"минимум: <b>${minimum}</b>" if minimum else "минимум: не проверен"
    )
    for plan in billing.unpurchasable_plans():
        lines.append(f"⚠️ {plan.id} (${_fmt_price(plan.price_usd)}) ниже минимума")
    lines.append("")
    with bot_db.db() as conn:
        rows = conn.execute(
            "SELECT payment_id, user_id, plan, status, price_amount, created_at, credited_at "
            "FROM payments ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    if not rows:
        lines.append("• платежей ещё не было")
    for row in rows:
        mark = "✅" if row["credited_at"] else "•"
        lines.append(
            f"{mark} <code>{esc(str(row['payment_id']))}</code> "
            f"u{row['user_id']} {esc(str(row['plan']))} ${esc(str(row['price_amount']))} "
            f"{esc(str(row['status']))} {esc(str(row['created_at']))[:16]}"
        )
    return "\n".join(lines)
