"""Simple in-memory rate limits (message cooldown, bulk_file)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from bot.i18n import t

# defaults: max events per window_sec
LIMITS = {
    "submit": (30, 60.0),       # 30 submits / 60s (bulk lines count individually lightly)
    "bulk_file": (5, 3600.0),   # 5 data files / hour
    "proxy_file": (10, 3600.0), # 10 proxy uploads / hour
    "broadcast_msg": (1, 0.05), # internal spacing helper
    "pay_check": (6, 60.0),     # manual «check payment» taps / minute
    "pay_create": (5, 3600.0),  # new payments / hour (one open payment is reused)
    "key_upload": (5, 3600.0),  # captcha key uploads / hour (live validation)
}

# action -> user_id -> deque of timestamps
_hits: dict[str, dict[int, deque[float]]] = defaultdict(lambda: defaultdict(deque))
_lock = threading.Lock()

# Bound on per-user entries: a one-visit user must not stay resident forever
# (only a fresh hit of the SAME user prunes their own deque otherwise).
_HITS_USERS_MAX = 10_000
_HITS_PRUNE_INTERVAL = 300.0
_hits_last_prune = 0.0


def _prune_hits(now: float) -> None:
    """Drop empty per-user deques; whole-action maps when they empty out."""
    global _hits_last_prune
    if now - _hits_last_prune < _HITS_PRUNE_INTERVAL:
        return
    _hits_last_prune = now
    users_total = sum(len(bucket) for bucket in _hits.values())
    if users_total <= _HITS_USERS_MAX:
        return
    for action, bucket in list(_hits.items()):
        for uid in list(bucket.keys()):
            q = bucket[uid]
            # Keep only what any window could still count: drop entries older
            # than the longest window, then drop the user if nothing is left.
            _, window = LIMITS.get(action, (60, 60.0))
            while q and now - q[0] > window:
                q.popleft()
            if not q:
                bucket.pop(uid, None)
        if not bucket:
            _hits.pop(action, None)


def allow(user_id: int, action: str, *, lang: str = "ru") -> tuple[bool, str]:
    """Return (ok, error_message)."""
    max_n, window = LIMITS.get(action, (60, 60.0))
    now = time.time()
    uid = int(user_id)
    with _lock:
        _prune_hits(now)
        q = _hits[action][uid]
        while q and now - q[0] > window:
            q.popleft()
        if not q:
            # A user whose window is empty frees their entry right away.
            _hits[action].pop(uid, None)
            q = _hits[action][uid]
        if len(q) >= max_n:
            return (
                False,
                t("rate.too_fast", lang),
            )
        q.append(now)
        return True, ""


# --- Per-user cooldown on incoming messages (config.MESSAGE_COOLDOWN_SEC) ---
#
# Deliberately not built on `allow()`: this gate runs on *every* message before
# any handler, so it stores two floats per user instead of a deque, and it says
# how long is left and whether the user has already been told.
#
# user_id -> (last_accepted_ts, last_warned_ts)
_msg_gate: dict[int, tuple[float, float]] = {}
_MSG_GATE_MAX = 10_000


def message_gate(user_id: int, cooldown: float) -> tuple[bool, float, bool]:
    """One incoming message per `cooldown` seconds per user.

    Returns `(allowed, seconds_left, should_warn)`. `should_warn` is True at
    most once per window: a flood is dropped in silence rather than answered
    message for message, because answering is what a flood is trying to buy.
    """
    if cooldown <= 0:
        return True, 0.0, False
    now = time.time()
    uid = int(user_id)
    with _lock:
        if len(_msg_gate) > _MSG_GATE_MAX:
            stale = now - max(cooldown * 10.0, 600.0)
            for k, (last, _w) in list(_msg_gate.items()):
                if last < stale:
                    _msg_gate.pop(k, None)
        last, warned = _msg_gate.get(uid, (0.0, 0.0))
        left = cooldown - (now - last)
        if left <= 0:
            _msg_gate[uid] = (now, 0.0)
            return True, 0.0, False
        should_warn = (now - warned) >= cooldown
        if should_warn:
            _msg_gate[uid] = (last, now)
        return False, left, should_warn


def reset_user(user_id: int) -> None:
    uid = int(user_id)
    with _lock:
        for action in list(_hits.keys()):
            _hits[action].pop(uid, None)
        _msg_gate.pop(uid, None)
