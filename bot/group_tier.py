"""Group membership → subscriber OK limit, resolved live with getChatMember.

`active_subscribers.txt` is a hand-edited file; this is the same tier granted
by being in one Telegram group. The Bot API cannot list a group's members, so
membership is a question asked per user and remembered for a while, exactly
like the channel gate in `bot/channel.py`.

Config: `SUBSCRIBER_GROUP` (empty = off), `SUBSCRIBER_GROUP_OK_LIMIT`
(0 = follow `SUBSCRIBER_OK_LIMIT`). The bot must be an administrator of the
group, otherwise every answer is an error and nobody gets the tier.
"""

from __future__ import annotations

import threading
import time

from bot import config

# ChatMember statuses that mean "still in the group".
_IN_GROUP = frozenset({"creator", "administrator", "member", "restricted"})

_lock = threading.Lock()
# user_id → (expires_at_monotonic, is_member)
_cache: dict[int, tuple[float, bool]] = {}
# Bounded like the channel cache: it is a saved API call, not a source of truth.
_CACHE_MAX = 20000

_bot = None
_bot_lock = threading.Lock()
_last_error = ""
_last_error_at = 0.0
_ERROR_LOG_INTERVAL = 60.0


def enabled() -> bool:
    return bool((getattr(config, "SUBSCRIBER_GROUP", "") or "").strip())


def subscriber_ok_limit() -> int:
    """SUBSCRIBER_OK_LIMIT with 0 passed through (0 = tier disabled).

    ``or 100`` is forbidden here (AGENTS 16.5): it would turn an explicit
    0 into a 100-slot grant. Only None/empty falls back to the default.
    """
    raw = getattr(config, "SUBSCRIBER_OK_LIMIT", 100)
    if raw is None or raw == "":
        return 100
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 100


def member_ok_limit() -> int:
    """OK slots a member of the group gets."""
    own = int(getattr(config, "SUBSCRIBER_GROUP_OK_LIMIT", 0) or 0)
    if own > 0:
        return own
    return subscriber_ok_limit()


def bind(bot) -> None:
    """Give the module the polling bot instance (called from create_bot)."""
    global _bot
    with _bot_lock:
        _bot = bot


def _get_bot():
    """The polling bot, or None in a process that never bound one.

    Deliberately no fallback client: a process that does not talk to Telegram
    (the standalone read-only mini app, tools, the test suite) must not start
    spending the bot's API budget on membership questions just because it
    imported `bot.limits`. Without a bot the tier is simply not granted.
    """
    with _bot_lock:
        if _bot is None:
            return None
        return _bot


def _log_error(detail: str) -> None:
    """One line per minute — a broken group must not flood the log."""
    global _last_error, _last_error_at
    now = time.monotonic()
    if detail == _last_error and (now - _last_error_at) < _ERROR_LOG_INTERVAL:
        return
    _last_error, _last_error_at = detail, now
    print(f"[group_tier] {config.SUBSCRIBER_GROUP}: {detail}", flush=True)


def _cached(uid: int) -> tuple[bool, bool]:
    """Returns (hit, value). A stale entry is still returned as a miss+value."""
    with _lock:
        entry = _cache.get(uid)
    if entry is None:
        return False, False
    return (entry[0] > time.monotonic()), entry[1]


def _store(uid: int, value: bool, ttl: float, *, source: str = "") -> None:
    """Remember an answer, and say so in the log when it is news.

    Only transitions are logged — a re-confirmation every ten minutes would
    say nothing and bury the lines that matter. This is how an operator sees
    that the tier is actually being handed out.
    """
    with _lock:
        previous = _cache.get(uid)
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
            previous = None
        _cache[uid] = (time.monotonic() + max(1.0, ttl), value)
    if previous is not None and previous[1] == value:
        return
    where = f" ({source})" if source else ""
    if value:
        print(f"[group_tier] {uid} is in the group → OK limit {member_ok_limit()}{where}", flush=True)
    elif previous is not None:
        print(f"[group_tier] {uid} is no longer in the group → tier dropped{where}", flush=True)


def invalidate(user_id: int | None = None) -> None:
    """Forget one cached answer (or all of them)."""
    with _lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(int(user_id), None)


def _invalidate_limits(uid: int) -> None:
    try:
        from bot import limits as bot_limits

        bot_limits.invalidate(uid)
    except Exception:  # pragma: no cover - cache drop must not break anything
        pass


def note_membership(user_id: int, joined: bool) -> None:
    """Record a join/leave seen in the group itself — no API call needed."""
    if not enabled():
        return
    uid = int(user_id)
    _store(uid, bool(joined), member_ttl() if joined else nonmember_ttl(), source="event")
    _invalidate_limits(uid)


def member_ttl() -> float:
    return max(1.0, float(getattr(config, "GROUP_MEMBER_TTL", 600) or 600))


def nonmember_ttl() -> float:
    return max(1.0, float(getattr(config, "GROUP_NONMEMBER_TTL", 30) or 30))


def matches_chat(chat_id: int | str | None, username: str | None = None) -> bool:
    """True when an update comes from the configured group.

    `SUBSCRIBER_GROUP` may be either a numeric id or an @username, so both
    forms are compared.
    """
    target = (getattr(config, "SUBSCRIBER_GROUP", "") or "").strip()
    if not target:
        return False
    if chat_id is not None and str(chat_id).strip() == target:
        return True
    if username:
        return ("@" + str(username).lstrip("@")).lower() == target.lower()
    return False


def is_member(user_id: int) -> bool:
    """True when this user is in the configured group.

    Never raises: a Telegram failure answers with the last known value, or
    False when there is none. The tier can only be granted, never taken away,
    by this function being wrong — a wrong False costs slots the user can get
    back by tapping again, a wrong True would hand out a limit nobody has.
    """
    if not enabled():
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False

    fresh, cached = _cached(uid)
    if fresh:
        return cached

    bot = _get_bot()
    if bot is None:
        return cached

    try:
        member = bot.get_chat_member(config.SUBSCRIBER_GROUP, uid)
    except Exception as e:
        desc = (getattr(e, "description", None) or str(e)).lower()
        if "user not found" in desc or "participant_id_invalid" in desc:
            # A real answer: Telegram knows the chat and this user is not in it.
            _store(uid, False, nonmember_ttl())
            return False
        # Everything else (lost admin rights, chat not found, network) is our
        # problem, not the user's: hold the previous answer instead of
        # demoting everybody to free while it is being fixed.
        _log_error(str(e)[:200])
        return cached

    status = (getattr(member, "status", None) or "").strip()
    ok = status in _IN_GROUP
    if ok and status == "restricted":
        # A restricted user may already have left; Telegram says so explicitly.
        ok = bool(getattr(member, "is_member", True))
    _store(uid, ok, member_ttl() if ok else nonmember_ttl(), source=status or "api")
    return ok


def note_service_message(message) -> bool:
    """Read a join/leave service message from the group; True if it was ours.

    The bot sits in the group anyway, so these updates are free — without them
    a new member waits for the membership cache to expire before the tier
    reaches them.
    """
    if not enabled():
        return False
    try:
        chat = getattr(message, "chat", None)
        if chat is None or not matches_chat(
            getattr(chat, "id", None), getattr(chat, "username", None)
        ):
            return False
        seen = False
        for user in (getattr(message, "new_chat_members", None) or []):
            if not getattr(user, "is_bot", False):
                note_membership(user.id, True)
                seen = True
        left = getattr(message, "left_chat_member", None)
        if left is not None and not getattr(left, "is_bot", False):
            note_membership(left.id, False)
            seen = True
        return seen
    except Exception as exc:  # a service message must never break polling
        _log_error(f"service message: {exc}")
        return False


def cached_member_count() -> int:
    """Members seen recently — for /admin, never a source of truth."""
    now = time.monotonic()
    with _lock:
        return sum(1 for expires, value in _cache.values() if value and expires > now)
