"""Durable outbound queue for anything the bot sends to many users at once.

Why a queue rather than a ``for`` loop over ``send_message``:

* **429 is real.** Telegram answers a burst with ``Too Many Requests`` and a
  ``retry_after``. A loop that swallows the exception drops that message for
  good and the delivery count lies about it. Here the row goes back to
  ``pending`` with ``next_attempt_at`` set, and the whole sender holds off for
  as long as Telegram asked.
* **403 is not a failure to retry.** «bot was blocked by the user» stays true
  until that user writes first, so the row ends as ``blocked`` and the user is
  flagged (``users.blocked_bot``) instead of being retried on every later batch.
* **A restart must not double-send.** Rows live in SQLite and the unique
  ``(batch_id, user_id)`` index makes enqueuing idempotent, so a service
  restart in the middle of a batch resumes exactly where it stopped.

Pacing has two independent knobs:

* ``interval_sec`` per batch — the deliberate drip. A new-project alert uses 10s
  so 130 people do not all pile into the same project queue in the same minute.
  Rows go out best tier first (see :data:`TIER_RANK`), so paid users get the
  head start the pacing is there to create.
* a global floor shared by every batch, which is what actually keeps the bot
  under Telegram's own rate limit when a fast manual broadcast runs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from bot import db as bot_db

# Tier ladder from bot/limits.py, best first: a batch drains in this order.
# "pro" is a retired tier (removed 2026-08-17) and `limit_mode` can no longer
# return it. The row stays so that a queued batch written before the removal
# still sorts where its author meant it to, rather than silently dropping to
# the free rank mid-drain.
TIER_RANK = {
    "vip": 0,
    "pro": 1,
    "basic": 2,
    "own": 3,
    "subscriber": 4,
    "free": 5,
}
DEFAULT_RANK = TIER_RANK["free"]

# Telegram tolerates ~30 messages/second bot-wide; 20/s leaves headroom for the
# ordinary traffic of a bot that is also answering people.
GLOBAL_MIN_INTERVAL = float(os.environ.get("BROADCAST_MIN_INTERVAL", "0.05"))
# How long the sender naps when the queue is empty.
IDLE_SLEEP = float(os.environ.get("BROADCAST_IDLE_SLEEP", "2.0"))
MAX_ATTEMPTS = int(os.environ.get("BROADCAST_MAX_ATTEMPTS", "5"))
RETRY_BASE = float(os.environ.get("BROADCAST_RETRY_BASE", "5.0"))
RETRY_CAP = float(os.environ.get("BROADCAST_RETRY_CAP", "300.0"))

_stop = threading.Event()
_thread: threading.Thread | None = None
# batch_id -> monotonic timestamp of its last send (per-batch drip)
_last_batch_send: dict[str, float] = {}
_last_send = 0.0
# Set by a 429: no row of any batch goes out before this monotonic deadline.
_pause_until = 0.0

# Telegram descriptions that mean «this chat will never accept our messages».
_DEAD_CHAT = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "peer_id_invalid",
    "bot can't initiate conversation",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _later(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))).isoformat()


def tier_rank(user_id: int) -> int:
    """Queue position for a user: VIP first, free last."""
    try:
        from bot import limits as bot_limits

        return TIER_RANK.get(bot_limits.limit_mode(int(user_id)), DEFAULT_RANK)
    except Exception:
        return DEFAULT_RANK


def build_markup_json(buttons: list[list[dict]]) -> str:
    """Serialize an inline keyboard spec: ``[[{"text":…, "callback_data":…}]]``."""
    return json.dumps(buttons, ensure_ascii=False)


def _markup_from_json(raw: str):
    if not (raw or "").strip():
        return None
    try:
        spec = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(spec, list) or not spec:
        return None
    from telebot import types

    kb = types.InlineKeyboardMarkup()
    for line in spec:
        if not isinstance(line, list):
            continue
        row = [
            types.InlineKeyboardButton(
                text=str(btn.get("text") or "?"),
                url=btn.get("url") or None,
                callback_data=btn.get("callback_data") or None,
            )
            for btn in line
            if isinstance(btn, dict)
        ]
        if row:
            kb.row(*row)
    return kb if kb.keyboard else None


def enqueue(
    batch_id: str,
    user_ids: list[int],
    *,
    body: str = "",
    body_for=None,
    parse_mode: str = "",
    markup_json: str = "",
    markup_for=None,
    interval_sec: float = 0.0,
) -> int:
    """Queue one batch. Returns how many rows were added (duplicates ignored).

    ``body_for(user_id)`` / ``markup_for(user_id)`` render per recipient — that
    is how one batch goes out in each user's own language while still draining
    at a single pace.
    """
    seen: set[int] = set()
    recipients: list[dict] = []
    for uid in user_ids:
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        if uid in seen:
            continue
        seen.add(uid)
        recipients.append(
            {
                "user_id": uid,
                "rank": tier_rank(uid),
                "body": body_for(uid) if body_for else body,
                "markup_json": markup_for(uid) if markup_for else markup_json,
            }
        )
    if not recipients:
        return 0
    return bot_db.enqueue_broadcast_rows(
        batch_id,
        recipients,
        parse_mode=parse_mode,
        interval_sec=interval_sec,
    )


def _retry_after(exc) -> float:
    """Seconds Telegram asked us to wait, 0 when it did not say."""
    data = getattr(exc, "result_json", None)
    if isinstance(data, dict):
        params = data.get("parameters")
        if isinstance(params, dict):
            try:
                return max(0.0, float(params.get("retry_after") or 0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def send_row(bot, row: dict) -> tuple[str, str]:
    """Deliver one row. Returns ``(state, error)``; state ``pending`` = retry later.

    Split out of the loop so tests can drive it with a fake bot.
    """
    global _pause_until

    from telebot.apihelper import ApiTelegramException

    attempts = int(row.get("attempts") or 0) + 1
    try:
        bot.send_message(
            int(row["user_id"]),
            str(row["body"]),
            parse_mode=(row.get("parse_mode") or None),
            reply_markup=_markup_from_json(str(row.get("markup_json") or "")),
            disable_web_page_preview=True,
        )
        return "sent", ""
    except ApiTelegramException as exc:
        code = int(getattr(exc, "error_code", 0) or 0)
        desc = str(getattr(exc, "description", "") or exc).lower()
        if code == 429:
            wait = _retry_after(exc) or RETRY_BASE
            # Hold the whole sender back, not just this row: the next recipient
            # would hit the same wall.
            _pause_until = max(_pause_until, time.monotonic() + wait)
            bot_db.mark_broadcast_row(
                int(row["id"]),
                "pending",
                error=f"429 retry_after={wait:g}",
                attempts=int(row.get("attempts") or 0),
                next_attempt_at=_later(wait),
            )
            return "pending", f"429 retry_after={wait:g}"
        if code == 403 or any(phrase in desc for phrase in _DEAD_CHAT):
            try:
                bot_db.set_bot_blocked(int(row["user_id"]), True)
            except Exception:
                pass
            return "blocked", desc[:200]
        if 400 <= code < 500:
            # Malformed text / bad markup: another attempt sends the same bytes.
            return "failed", f"{code} {desc}"[:200]
        return _retry_or_fail(row, attempts, f"{code} {desc}")
    except Exception as exc:  # network blip, DNS, proxy — worth another try
        return _retry_or_fail(row, attempts, str(exc))


def _retry_or_fail(row: dict, attempts: int, error: str) -> tuple[str, str]:
    if attempts >= MAX_ATTEMPTS:
        return "failed", f"{error}"[:200]
    delay = min(RETRY_CAP, RETRY_BASE * (2 ** (attempts - 1)))
    bot_db.mark_broadcast_row(
        int(row["id"]),
        "pending",
        error=str(error)[:200],
        attempts=attempts,
        next_attempt_at=_later(delay),
    )
    return "pending", str(error)[:200]


def _pick(rows: list[dict]) -> tuple[dict | None, float]:
    """First row whose batch is off cooldown, else the shortest wait to try again."""
    now = time.monotonic()
    soonest = IDLE_SLEEP
    for row in rows:
        batch = str(row.get("batch_id") or "")
        interval = float(row.get("interval_sec") or 0.0)
        if interval <= 0:
            return row, 0.0
        last = _last_batch_send.get(batch)
        if last is None or now - last >= interval:
            return row, 0.0
        soonest = min(soonest, interval - (now - last))
    return None, max(0.1, soonest)


def drain_once(bot, *, limit: int = 50) -> str:
    """One step of the sender. Returns what happened, for logs and tests."""
    global _last_send

    now = time.monotonic()
    if now < _pause_until:
        time.sleep(min(_pause_until - now, 5.0))
        return "paused"

    rows = bot_db.next_broadcast_rows(limit)
    if not rows:
        return "empty"

    row, wait = _pick(rows)
    if row is None:
        time.sleep(min(wait, IDLE_SLEEP))
        return "cooldown"

    gap = time.monotonic() - _last_send
    if gap < GLOBAL_MIN_INTERVAL:
        time.sleep(GLOBAL_MIN_INTERVAL - gap)

    state, error = send_row(bot, row)
    _last_send = time.monotonic()
    _last_batch_send[str(row.get("batch_id") or "")] = _last_send
    if state != "pending":
        # `pending` rows were already rescheduled with their own deadline.
        bot_db.mark_broadcast_row(
            int(row["id"]),
            state,
            error=error,
            attempts=int(row.get("attempts") or 0) + 1,
        )
    return state


def start_sender(bot) -> threading.Thread:
    """Single background sender. Idempotent: a second call is a no-op."""
    global _thread

    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()

    def _loop() -> None:
        while not _stop.is_set():
            try:
                result = drain_once(bot)
                if result == "empty":
                    _stop.wait(IDLE_SLEEP)
            except Exception as exc:  # noqa: BLE001 — the sender must not die
                print(f"[broadcast] sender error: {exc}", flush=True)
                _stop.wait(IDLE_SLEEP)

    _thread = threading.Thread(target=_loop, name="broadcast-sender", daemon=True)
    _thread.start()
    return _thread


def stop_sender() -> None:
    _stop.set()


def stats(batch_id: str) -> dict[str, int]:
    return bot_db.broadcast_batch_stats(batch_id)


def cancel(batch_id: str) -> int:
    _last_batch_send.pop(str(batch_id), None)
    return bot_db.cancel_broadcast_batch(batch_id)


def format_stats(batch_id: str) -> str:
    s = stats(batch_id)
    return (
        f"отправлено {s.get('sent', 0)}/{s.get('total', 0)} · "
        f"в очереди {s.get('pending', 0)} · "
        f"заблокировали бота {s.get('blocked', 0)} · "
        f"ошибок {s.get('failed', 0)}"
    )
