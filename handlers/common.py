"""Gate/screen helpers shared by every handler group.

These were small closures inside ``create_bot``; as plain functions taking
``bot`` explicitly they can serve handler modules outside it. The bodies are
moved verbatim — only the ``bot`` parameter is new.
"""

from __future__ import annotations

import telebot
from telebot import types

from bot import admin_ux
from bot import config
from bot import db as bot_db
from bot import screen
from bot.i18n import t
from bot.channel import require_subscription
from bot.ux import safe_send


def is_admin(uid: int) -> bool:
    # Fail-closed: empty ADMIN_IDS denies admin (unless ALLOW_OPEN_ADMIN=1 for dev)
    if not config.ADMIN_IDS:
        return bool(getattr(config, "ALLOW_OPEN_ADMIN", False))
    return uid in config.ADMIN_IDS


def deny_if_banned(bot: telebot.TeleBot, message: types.Message) -> bool:
    """If banned, reply and return True (caller should stop)."""
    if bot_db.is_banned(message.from_user.id):
        bot.reply_to(message, admin_ux.banned_user_text())
        return True
    return False


def deny_if_banned_call(bot: telebot.TeleBot, call: types.CallbackQuery) -> bool:
    if bot_db.is_banned(call.from_user.id):
        lang = bot_db.get_lang(call.from_user.id)
        screen.toast(bot, call, t("toast.banned", lang), alert=True)
        return True
    return False


def show_screen(
    bot: telebot.TeleBot,
    user_id: int,
    chat_id: int,
    state: str,
    params: dict | None = None,
    *,
    lang: str | None = None,
    call: types.CallbackQuery | None = None,
    toast: str = "",
    alert: bool = False,
    force_new: bool = False,
    view: screen.View | None = None,
) -> int | None:
    """Draw a screen on this user's anchored message (see bot/screen.py)."""
    return screen.show(
        bot,
        user_id,
        chat_id,
        state,
        params,
        lang=lang,
        call=call,
        toast_text=toast,
        alert=alert,
        force_new=force_new,
        view=view,
    )


def screen_gate(
    bot: telebot.TeleBot,
    call: types.CallbackQuery,
    user_id: int,
    deny_text: str | None,
    *,
    toast: str | None = None,
) -> None:
    lang = bot_db.get_lang(user_id)
    show_screen(
        bot,
        user_id,
        call.message.chat.id,
        screen.STATE_GATE,
        {"text": deny_text or ""},
        lang=lang,
        call=call,
        toast=toast if toast is not None else t("toast.need_sub", lang),
        alert=True,
    )


def gate_sub(
    bot: telebot.TeleBot, user_id: int, lang: str | None = None
) -> tuple[bool, str | None, types.InlineKeyboardMarkup | None]:
    if lang is None:
        try:
            lang = bot_db.get_lang(user_id)
        except Exception:
            lang = "ru"
    return require_subscription(bot, user_id, lang=lang)


def admin_notify(bot: telebot.TeleBot, admin_id: int, text: str) -> None:
    """Send one admin an HTML notice (payments, alerts). Never raises."""
    try:
        bot.send_message(
            admin_id, text, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception as exc:
        print(f"[admin] notify {admin_id} failed: {exc}", flush=True)


def admin_edit(
    bot: telebot.TeleBot, call: types.CallbackQuery, text: str, kb=None
) -> None:
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        safe_send(
            bot,
            call.message.chat.id,
            text,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
