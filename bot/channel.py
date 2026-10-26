"""Telegram channel membership gate for the bot."""

from __future__ import annotations

import threading
import time
from typing import Literal

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

from bot import config
from bot.i18n import t
from bot.ux import esc

# Telegram ChatMember statuses that mean "still in the channel"
_IN_CHANNEL = frozenset({"creator", "administrator", "member", "restricted"})

CheckResult = Literal["ok", "not_member", "error"]


def channel_link_html() -> str:
    url = esc(config.REQUIRED_CHANNEL_URL)
    title = esc(config.REQUIRED_CHANNEL_TITLE or config.REQUIRED_CHANNEL)
    return f'<a href="{url}">{title}</a>'


def subscribe_text(extra: str = "", lang: str | None = None) -> str:
    """Message when user is not subscribed."""
    return t("gate.text", lang, link=channel_link_html()) + (
        ("\n\n" + extra) if extra else ""
    )


def subscribe_keyboard(lang: str | None = None) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            text=t("gate.subscribe_btn", lang),
            url=config.REQUIRED_CHANNEL_URL,
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            text=t("gate.check_btn", lang),
            callback_data="subcheck",
        )
    )
    return kb


def check_subscription(
    bot: telebot.TeleBot, user_id: int, *, lang: str | None = None
) -> tuple[CheckResult, str]:
    """
    Returns (result, detail).
    result: ok | not_member | error
    """
    if not config.REQUIRE_CHANNEL:
        return "ok", "disabled"
    if not config.REQUIRED_CHANNEL:
        # No channel configured (public default) — nothing to check against.
        return "ok", "not configured"

    try:
        member = bot.get_chat_member(config.REQUIRED_CHANNEL, user_id)
        status = getattr(member, "status", None) or ""
        if status in _IN_CHANNEL:
            return "ok", status
        return "not_member", status or "unknown"
    except ApiTelegramException as e:
        desc = (getattr(e, "description", None) or str(e)).lower()
        # User never interacted / not in chat
        if "user not found" in desc or "chat not found" in desc:
            return "not_member", str(e)
        if "bot is not a member" in desc or "not enough rights" in desc:
            return (
                "error",
                t(
                    "gate.bot_not_admin",
                    lang,
                    channel=esc(config.REQUIRED_CHANNEL),
                ),
            )
        return "error", str(e)
    except Exception as e:
        return "error", str(e)


# A chat handler asks about membership a few times per tap, which `getChatMember`
# absorbs. The mini app asks once per poll — every four seconds, per open
# screen — and that is a different thing entirely: it would spend the bot's
# Telegram budget on a question whose answer changes maybe once a week.
#
# So the web path gets a cached read, and the chat path keeps the uncached one.
# The two TTLs are asymmetric on purpose. A "yes" is safe to hold: someone who
# leaves the channel keeps their access for at most five minutes, which costs
# nothing. A "no" must expire fast, because the user reading it is on their way
# to the channel right now and will come straight back — and `subscribe_now`
# drops it the moment they say they have joined, so the wait is a fallback for
# a user who never presses anything, not the normal path.
_SUB_OK_TTL_S = 300.0
_SUB_NO_TTL_S = 15.0

_sub_cache: dict[int, tuple[float, CheckResult, str]] = {}
_sub_lock = threading.Lock()
# Beyond this the cache is dropped whole rather than grown without bound; it is
# a saved API call, not a source of truth, so losing it costs one extra read.
_SUB_CACHE_MAX = 20000


def invalidate_subscription(user_id: int | None = None) -> None:
    """Forget a cached answer — call it when the user says they just joined."""
    with _sub_lock:
        if user_id is None:
            _sub_cache.clear()
            return
        _sub_cache.pop(int(user_id), None)


def check_subscription_cached(
    bot: telebot.TeleBot, user_id: int, *, lang: str | None = None
) -> tuple[CheckResult, str]:
    """`check_subscription` with a short memory, for callers that poll.

    `error` is never cached: it usually means the bot lost its administrator
    rights on the channel, and that is fixed by an operator within minutes.
    Holding it would keep refusing users for five minutes after the fix.
    """
    if not config.REQUIRE_CHANNEL:
        return "ok", "disabled"
    uid = int(user_id)
    now = time.monotonic()
    with _sub_lock:
        hit = _sub_cache.get(uid)
        if hit is not None and hit[0] > now:
            return hit[1], hit[2]

    result, detail = check_subscription(bot, user_id, lang=lang)
    if result == "error":
        return result, detail

    ttl = _SUB_OK_TTL_S if result == "ok" else _SUB_NO_TTL_S
    with _sub_lock:
        if len(_sub_cache) >= _SUB_CACHE_MAX:
            _sub_cache.clear()
        _sub_cache[uid] = (now + ttl, result, detail)
    return result, detail


def require_subscription(
    bot: telebot.TeleBot,
    user_id: int,
    *,
    lang: str | None = None,
) -> tuple[bool, str | None, types.InlineKeyboardMarkup | None]:
    """
    Gate helper for handlers.
    Returns (allowed, error_html_or_none, reply_markup_or_none).
    """
    result, detail = check_subscription(bot, user_id, lang=lang)
    if result == "ok":
        return True, None, None
    if result == "not_member":
        return False, subscribe_text(lang=lang), subscribe_keyboard(lang=lang)
    # error — show to user (and admin should fix bot rights)
    return (
        False,
        subscribe_text(extra=t("gate.error", lang, detail=esc(detail)), lang=lang),
        subscribe_keyboard(lang=lang),
    )
