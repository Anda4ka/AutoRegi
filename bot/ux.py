"""Telegram HTML helpers and safe send/edit."""

from __future__ import annotations

import html
import io
import re

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException


def esc(s: object) -> str:
    """Escape for Telegram HTML parse_mode."""
    return html.escape(str(s) if s is not None else "", quote=False)


def is_busy_detail(detail: str = "") -> bool:
    """
    True only for real server-side lock / sheet contention.

    Do NOT call is_retryable_failure(..., reason="gas_busy"): that reason short-circuits
    to True for almost any detail and forges BAD_RESP/FAIL into "busy" UX.
    """
    try:
        from bot.http_client import is_gas_lock_message

        if is_gas_lock_message(detail or ""):
            return True
    except Exception:
        pass
    low = (detail or "").lower()
    return any(
        x in low
        for x in (
            "lock timeout",
            "lock service",
            "spreadsheet lock",
            "holding the lock",
            "server busy after retries",
            "หมดเวลา",
            "ล็อก",
        )
    )


def safe_detail(detail: str, limit: int = 160) -> str:
    """
    Detail may be a full HTML error page — never inject raw tags into TG HTML.
    Lock/busy messages are normalized to short Russian (no Thai/EN dump).
    """
    d = (detail or "").strip()
    if not d:
        return "—"
    if is_busy_detail(d):
        return "сервер занят (Google Sheet lock) — попробуй ещё раз"
    low = d.lstrip()[:20].lower()
    if low.startswith("<!doctype") or low.startswith("<html") or "<body" in d[:200].lower():
        return "server returned HTML error page (not JSON)"
    d = " ".join(d.split())
    d = d.replace("<", "‹").replace(">", "›")
    if len(d) > limit:
        d = d[:limit] + "…"
    return d


_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def strip_html_tags(text: str) -> str:
    """Fallback for a parse failure: remove every tag, not just b/code/i.

    A bare `<` or an unusual tag surviving the strip would fail the plain-text
    retry too, and the message would be lost.
    """
    return _TAG_RE.sub("", text)


def safe_send(bot: telebot.TeleBot, chat_id: int, text: str, **kwargs) -> None:
    """Send HTML message; on entity parse errors fall back to plain text."""
    try:
        bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
    except ApiTelegramException as e:
        if "can't parse entities" in str(e).lower() or "parse entities" in str(e).lower():
            bot.send_message(chat_id, strip_html_tags(text), parse_mode=None, **kwargs)
        else:
            raise


def safe_send_document(
    bot: telebot.TeleBot,
    chat_id: int,
    filename: str,
    content: bytes | str,
    caption: str = "",
) -> int | None:
    """Send a file. Returns its message_id, or None if it did not arrive.

    Unlike :func:`safe_send` this reports failure instead of swallowing it: the
    caller may be about to destroy the only copy of what it just sent, and must
    be able to tell a delivered file from a lost one.
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    stream = io.BytesIO(content)
    stream.name = filename
    try:
        msg = bot.send_document(
            chat_id,
            stream,
            visible_file_name=filename,
            caption=(caption or None),
            parse_mode="HTML" if caption else None,
        )
    except (ApiTelegramException, OSError) as exc:
        print(f"[ux] send_document to {chat_id} failed: {exc}", flush=True)
        return None
    return getattr(msg, "message_id", None)


def safe_edit_or_reply(
    bot: telebot.TeleBot,
    message: types.Message,
    wait: types.Message | None,
    text: str,
    **kwargs,
) -> None:
    """Edit wait message or reply. kwargs passed to send/edit (e.g. reply_markup)."""
    if wait is not None:
        try:
            bot.edit_message_text(
                text,
                chat_id=wait.chat.id,
                message_id=wait.message_id,
                parse_mode="HTML",
                **kwargs,
            )
            return
        except ApiTelegramException:
            pass
        except Exception:
            pass
    try:
        safe_send(
            bot,
            message.chat.id,
            text,
            reply_to_message_id=message.message_id,
            **kwargs,
        )
    except Exception:
        plain = strip_html_tags(text)
        try:
            bot.send_message(
                message.chat.id,
                plain[:4000],
                parse_mode=None,
                **{k: v for k, v in kwargs.items() if k == "reply_markup"},
            )
        except Exception as e:
            print(f"safe_edit_or_reply failed: {e}")


def remaining(ok_count: int, ok_limit: int) -> int:
    return max(0, ok_limit - ok_count)


def progress_bar(done: int, total: int, width: int = 10) -> str:
    """Text progress bar: ██████░░░░ (monospace-friendly)."""
    width = max(4, int(width))
    if total <= 0:
        return "░" * width
    done = max(0, int(done))
    filled = round(min(1.0, done / float(total)) * width)
    if filled > width:
        filled = width
    return "█" * filled + "░" * (width - filled)
