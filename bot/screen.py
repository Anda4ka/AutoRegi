"""One anchored, editable message per user — the bot's whole UI surface.

Every navigation step edits the same Telegram message instead of appending a
new one, so the chat stops growing while the user clicks around. Confirmations
ride on ``answer_callback_query`` (a toast), which costs no message at all.

Renderers are registered by ``tgbot.py`` (they need the project registry), so
this module stays free of project knowledge: the payment poller and the submit
queue can redraw any screen without importing the Telegram shell.

Anchor bookkeeping lives in ``users.screen_*``, so it survives a restart.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from bot import config
from bot import db as bot_db
from bot.ux import strip_html_tags


STATE_HOME = "home"
STATE_PROJECT = "project"
STATE_QUEUE = "queue"
STATE_DATA = "data"
STATE_PROXY = "proxy"
STATE_LANG = "lang"
STATE_SUGGEST = "suggest"
STATE_PLANS = "plans"
STATE_PAY = "pay"
STATE_RESULT = "result"
STATE_GATE = "gate"
STATE_KEYS = "keys"
STATE_DATA_EDIT = "data_edit"
STATE_DATA_DEL = "data_del"
STATE_AUTOREG = "autoreg"
STATE_PRIVATKA = "privatka"

# Telegram refuses to edit very old messages; re-anchor before we get there.
_MAX_AGE_HOURS = float(getattr(config, "SCREEN_MAX_AGE_HOURS", 47) or 47)

_TOAST_LIMIT = 180

_EDIT_OK = ("message is not modified",)
_EDIT_GONE = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "message to be replied not found",
    "message identifier is not specified",
)
_EDIT_PARSE = ("can't parse entities", "can't parse message text")


@dataclass(frozen=True)
class View:
    """What a screen looks like right now."""

    text: str
    markup: Any = None
    disable_preview: bool = True


Renderer = Callable[[int, str, dict], View]

_renderers: dict[str, Renderer] = {}
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def register(state: str, fn: Renderer) -> None:
    """Bind a state name to its renderer (called once, from tgbot.main)."""
    _renderers[str(state)] = fn


def registered_states() -> list[str]:
    return sorted(_renderers)


def user_lock(user_id: int) -> threading.Lock:
    """Serialise anchor writers: a queue worker and a callback thread collide."""
    uid = int(user_id)
    with _locks_guard:
        lock = _locks.get(uid)
        if lock is None:
            lock = threading.Lock()
            _locks[uid] = lock
        return lock


def _lang_for(user_id: int, lang: str | None) -> str:
    if lang:
        return lang
    try:
        return bot_db.get_lang(int(user_id))
    except Exception:
        return "ru"


def render(
    user_id: int, state: str, params: dict | None = None, *, lang: str | None = None
) -> View | None:
    fn = _renderers.get(str(state))
    if fn is None:
        print(f"[screen] no renderer for state {state!r}", flush=True)
        return None
    try:
        return fn(int(user_id), _lang_for(user_id, lang), dict(params or {}))
    except Exception as exc:  # pragma: no cover - a broken screen must not kill the handler
        print(f"[screen] render {state} failed: {exc}", flush=True)
        return None


def anchor(user_id: int) -> tuple[int | None, int | None]:
    row = bot_db.get_screen(int(user_id))
    if not row:
        return None, None
    return row.get("chat_id") or None, row.get("message_id") or None


def current_state(user_id: int) -> tuple[str, dict]:
    row = bot_db.get_screen(int(user_id))
    if not row:
        return "", {}
    try:
        params = json.loads(row.get("params") or "{}")
    except (TypeError, ValueError):
        params = {}
    return str(row.get("state") or ""), params if isinstance(params, dict) else {}


def owns_anchor(user_id: int, state: str) -> bool:
    """True when the live screen is currently showing ``state``."""
    return current_state(user_id)[0] == str(state)


def clear_anchor(user_id: int) -> None:
    bot_db.clear_screen(int(user_id))


def toast(bot, call, text: str = "", *, alert: bool = False) -> None:
    """Answer a callback query — the spinner stops and nothing hits the chat."""
    if call is None:
        return
    try:
        bot.answer_callback_query(
            call.id, (text or "")[:_TOAST_LIMIT], show_alert=bool(alert)
        )
    except Exception:
        pass


def _anchor_is_stale(row: dict | None) -> bool:
    if not row:
        return True
    stamp = bot_db.parse_iso(str(row.get("updated_at") or ""))
    if stamp is None:
        return False
    age_h = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
    return age_h >= _MAX_AGE_HOURS


def _send(bot, chat_id: int, view: View) -> int | None:
    try:
        msg = bot.send_message(
            chat_id,
            view.text,
            parse_mode="HTML",
            reply_markup=view.markup,
            disable_web_page_preview=view.disable_preview,
        )
        return int(msg.message_id)
    except Exception as exc:
        desc = _describe(exc)
        if any(k in desc for k in _EDIT_PARSE):
            try:
                msg = bot.send_message(
                    chat_id,
                    strip_html_tags(view.text)[:4000],
                    reply_markup=view.markup,
                    disable_web_page_preview=view.disable_preview,
                )
                return int(msg.message_id)
            except Exception as exc2:
                print(f"[screen] send fallback failed: {exc2}", flush=True)
                return None
        print(f"[screen] send failed: {exc}", flush=True)
        return None


def _describe(exc: BaseException) -> str:
    return str(getattr(exc, "description", "") or exc).lower()


def _edit(bot, chat_id: int, message_id: int, view: View) -> bool:
    """True when the anchor now shows ``view``; False when it must be replaced."""
    try:
        bot.edit_message_text(
            view.text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            reply_markup=view.markup,
            disable_web_page_preview=view.disable_preview,
        )
        return True
    except Exception as exc:
        desc = _describe(exc)
        # Editing to identical content is a no-op, not a failure: treating it
        # as one is what used to spawn a duplicate message.
        if any(k in desc for k in _EDIT_OK):
            return True
        if any(k in desc for k in _EDIT_PARSE):
            try:
                bot.edit_message_text(
                    strip_html_tags(view.text)[:4000],
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=view.markup,
                    disable_web_page_preview=view.disable_preview,
                )
                return True
            except Exception:
                return False
        if any(k in desc for k in _EDIT_GONE):
            return False
        print(f"[screen] edit failed: {exc}", flush=True)
        return False


def show(
    bot,
    user_id: int,
    chat_id: int,
    state: str,
    params: dict | None = None,
    *,
    lang: str | None = None,
    call: Any = None,
    toast_text: str = "",
    alert: bool = False,
    force_new: bool = False,
    view: View | None = None,
) -> int | None:
    """Draw ``state`` on this user's live screen and return its message id.

    Answering the callback happens first so the client stops spinning while the
    render runs. ``force_new`` posts a fresh message and re-anchors on it —
    used when a background job must reach a user who navigated away.
    """
    uid = int(user_id)
    chat = int(chat_id)
    params = dict(params or {})
    if call is not None:
        toast(bot, call, toast_text, alert=alert)
    if view is None:
        view = render(uid, state, params, lang=lang)
    if view is None:
        return None

    with user_lock(uid):
        row = bot_db.get_screen(uid)
        target: int | None = None
        if row and int(row.get("chat_id") or 0) == chat:
            target = row.get("message_id")
        # A tap on an older message should redraw that message: it is what the
        # user is looking at. It then becomes the anchor.
        tapped = getattr(getattr(call, "message", None), "message_id", None)
        if tapped:
            target = int(tapped)
        elif _anchor_is_stale(row):
            target = None

        message_id: int | None = None
        if target is not None and not force_new:
            if _edit(bot, chat, int(target), view):
                message_id = int(target)
        if message_id is None:
            message_id = _send(bot, chat, view)
        if message_id is None:
            return None

        try:
            payload = json.dumps(params, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = "{}"
        try:
            bot_db.set_screen(uid, chat, message_id, str(state), payload)
        except Exception as exc:  # pragma: no cover - screen drawn, pointer lost
            print(f"[screen] anchor save failed: {exc}", flush=True)
        return message_id


def refresh(bot, user_id: int, *, lang: str | None = None) -> bool:
    """Redraw whatever the user is currently looking at (background updates)."""
    state, params = current_state(int(user_id))
    if not state:
        return False
    chat_id, _ = anchor(int(user_id))
    if not chat_id:
        return False
    return show(bot, int(user_id), int(chat_id), state, params, lang=lang) is not None


@dataclass
class Throttle:
    """Rate-limit progress redraws (Telegram punishes chatty edits)."""

    min_interval: float = 2.0
    _last: float = field(default=0.0, repr=False)

    def ready(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if force or (now - self._last) >= self.min_interval:
            self._last = now
            return True
        return False
