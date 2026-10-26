"""Per-user await modes and the broadcast audience picker.

Moved verbatim out of tgbot.py: this state is process-global and shared by
several handler groups (admin, text, documents, billing), so it lives in its
own module with one lock per structure. tgbot re-imports the names, so every
existing caller and test keeps working — which is also why the structures
must not be re-created anywhere else.
"""

from __future__ import annotations

import threading
import time

from bot import db as bot_db
from bot import subscribers as bot_subscribers

# Users waiting to send a free-text project suggestion
_awaiting_suggest: set[int] = set()
_awaiting_suggest_lock = threading.Lock()

# Users in /proxy upload mode (document = proxy list, not wallet bulk)
_awaiting_proxy: set[int] = set()
_awaiting_proxy_lock = threading.Lock()

# Users in /data upload mode (handle|wallet profile)
_awaiting_data: set[int] = set()
_awaiting_data_lock = threading.Lock()

# Admin multi-step input: user_id -> mode string
_awaiting_admin: dict[int, str] = {}
_awaiting_admin_lock = threading.Lock()
# Broadcast draft text per admin
_broadcast_draft: dict[int, str] = {}
# /gform draft per admin: the parsed form dict (JSON) awaiting the mapping reply
_gform_draft: dict[int, str] = {}
# Broadcast audiences offered on the confirm screen, in button order.
# "1" is the legacy callback suffix and keeps meaning «everyone».
BROADCAST_AUDIENCES = ("all", "subs", "paid")


def broadcast_audience(key: str) -> tuple[list[int], str]:
    """Resolve a broadcast audience key → (recipient ids, human label).

    Every audience is intersected with the bot's own user table: a Telegram id
    that never started the bot cannot be messaged, and counting it would make
    the delivery report look like a failure. Banned users are excluded by
    ``list_all_user_ids``. Unknown keys fall back to everyone, so the old
    ``admin:bcast_go:1`` callback keeps working.
    """
    known = bot_db.list_all_user_ids(exclude_banned=True)
    key = (key or "").strip().lower()
    if key == "subs":
        ids = set(bot_subscribers.subscriber_ids_snapshot())
        if not ids:
            # Normally loaded at startup / on /reload; load here rather than
            # silently report «0 recipients» for a file that is not empty.
            bot_subscribers.load_subscribers()
            ids = set(bot_subscribers.subscriber_ids_snapshot())
        return [uid for uid in known if uid in ids], "Подписчики"
    if key == "paid":
        active = bot_db.list_active_subscriptions(limit=10000)
        ids = {int(row["user_id"]) for row in active}
        return [uid for uid in known if uid in ids], "Платные тарифы"
    return list(known), "Все"


def _set_awaiting_suggest(user_id: int, on: bool) -> None:
    with _awaiting_suggest_lock:
        if on:
            _awaiting_suggest.add(user_id)
        else:
            _awaiting_suggest.discard(user_id)


def _is_awaiting_suggest(user_id: int) -> bool:
    with _awaiting_suggest_lock:
        return user_id in _awaiting_suggest


def _set_awaiting_proxy(user_id: int, on: bool) -> None:
    with _awaiting_proxy_lock:
        if on:
            _awaiting_proxy.add(user_id)
        else:
            _awaiting_proxy.discard(user_id)


def _is_awaiting_proxy(user_id: int) -> bool:
    with _awaiting_proxy_lock:
        return user_id in _awaiting_proxy


def _set_awaiting_data(user_id: int, on: bool) -> None:
    with _awaiting_data_lock:
        if on:
            _awaiting_data.add(user_id)
        else:
            _awaiting_data.discard(user_id)


def _is_awaiting_data(user_id: int) -> bool:
    with _awaiting_data_lock:
        return user_id in _awaiting_data


# captcha-key await: user_id -> (provider, deadline_monotonic)
_awaiting_key_lock = threading.Lock()
_awaiting_key: dict[int, tuple[str, float]] = {}
_KEY_AWAIT_TTL = 300.0


def _set_awaiting_key(user_id: int, provider: str | None) -> None:
    with _awaiting_key_lock:
        if provider:
            _awaiting_key[user_id] = (provider, time.monotonic() + _KEY_AWAIT_TTL)
        else:
            _awaiting_key.pop(user_id, None)


def _get_awaiting_key(user_id: int) -> str | None:
    with _awaiting_key_lock:
        item = _awaiting_key.get(user_id)
        if not item:
            return None
        provider, deadline = item
        if time.monotonic() > deadline:
            _awaiting_key.pop(user_id, None)
            return None
        return provider


# admin await: user_id -> (mode, deadline_monotonic)
_ADMIN_AWAIT_TTL = 300.0  # 5 minutes


def _set_admin_await(user_id: int, mode: str | None) -> None:
    with _awaiting_admin_lock:
        if mode:
            _awaiting_admin[user_id] = (mode, time.monotonic() + _ADMIN_AWAIT_TTL)
        else:
            _awaiting_admin.pop(user_id, None)


def _get_admin_await(user_id: int) -> str | None:
    with _awaiting_admin_lock:
        item = _awaiting_admin.get(user_id)
        if not item:
            return None
        mode, deadline = item
        if time.monotonic() > deadline:
            _awaiting_admin.pop(user_id, None)
            return None
        return mode


def _clear_user_modes(user_id: int) -> None:
    _set_awaiting_suggest(user_id, False)
    _set_awaiting_proxy(user_id, False)
    _set_awaiting_data(user_id, False)
    _set_awaiting_key(user_id, None)
    _set_admin_await(user_id, None)
