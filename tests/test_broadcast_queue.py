"""Durable broadcast queue: 429, 403, restart safety and tier-ordered pacing.

The loop this replaces sent inside the admin callback with `except: pass`, so a
flood-control answer silently dropped the message and the delivery count lied
about it. What must hold now:

  * enqueuing the same batch twice cannot double-send (unique batch_id+user_id);
  * 429 puts the row back to `pending` with Telegram's own retry_after and
    holds the whole sender — nothing is lost;
  * 403 ends the row as `blocked` and flags the user, instead of burning
    retries on a chat that will never accept a message;
  * a batch with an interval drips: the second row waits, and higher tiers go
    out first;
  * rows already `sent` are never picked again — a restart resumes, not repeats.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="bcast_queue_"))
_DB = _TMP / "t.db"

os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "t:t"
os.environ["SUBSCRIBERS_FILE"] = str(_TMP / "active_subscribers.txt")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["LIMITS_CACHE_TTL"] = "0"
# No sleeping in tests: the global floor is not what is under test here.
os.environ["BROADCAST_MIN_INTERVAL"] = "0"

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402

config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]

from bot import broadcast  # noqa: E402
from telebot.apihelper import ApiTelegramException  # noqa: E402


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class FakeBot:
    """Records deliveries; `fail_with` maps user_id → exception to raise once."""

    def __init__(self, fail_with: dict | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_with = dict(fail_with or {})

    def send_message(self, chat_id, text, **kwargs):
        exc = self.fail_with.pop(int(chat_id), None)
        if exc is not None:
            raise exc
        self.sent.append((int(chat_id), text))
        return True


def _api_error(code: int, description: str, **params) -> ApiTelegramException:
    payload = {"error_code": code, "description": description, "ok": False}
    if params:
        payload["parameters"] = params
    return ApiTelegramException("sendMessage", "raw", payload)


def _reset() -> None:
    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute("DELETE FROM broadcast_outbox")
        conn.execute("DELETE FROM users")
        conn.commit()
    broadcast._last_batch_send.clear()
    broadcast._last_send = 0.0
    broadcast._pause_until = 0.0


def _seed_users(*ids: int) -> None:
    for uid in ids:
        bot_db.ensure_user(uid, f"u{uid}", f"U{uid}")


def _row(batch: str, user_id: int) -> dict:
    with bot_db.db() as conn:
        row = conn.execute(
            "SELECT * FROM broadcast_outbox WHERE batch_id = ? AND user_id = ?",
            (batch, user_id),
        ).fetchone()
    return dict(row) if row else {}


def test_enqueue_is_idempotent() -> None:
    _reset()
    _seed_users(1, 2, 3)
    first = broadcast.enqueue("b1", [1, 2, 3], body="hi")
    second = broadcast.enqueue("b1", [1, 2, 3], body="hi")
    _ok(first == 3, f"first enqueue inserted {first}")
    _ok(second == 0, f"re-enqueue inserted {second} rows — would double-send")
    _ok(bot_db.broadcast_batch_stats("b1")["total"] == 3)


def test_duplicate_ids_collapse() -> None:
    _reset()
    _seed_users(1)
    _ok(broadcast.enqueue("b2", [1, 1, 1], body="hi") == 1)


def test_flood_control_reschedules_and_pauses() -> None:
    _reset()
    _seed_users(10)
    broadcast.enqueue("b3", [10], body="hi")
    bot = FakeBot({10: _api_error(429, "Too Many Requests", retry_after=7)})

    state = broadcast.drain_once(bot)
    _ok(state == "pending", f"429 must keep the row pending, got {state}")
    _ok(not bot.sent, "nothing was delivered")

    row = _row("b3", 10)
    _ok(row["state"] == "pending", row["state"])
    _ok(row["next_attempt_at"] > datetime.now(timezone.utc).isoformat(), row)
    _ok("429" in row["error"], row["error"])
    # The row is not ready yet, so the sender has nothing to do right now.
    _ok(bot_db.next_broadcast_rows(10) == [], "row must not be ready before its deadline")
    _ok(broadcast._pause_until > 0, "the whole sender must back off after a 429")

    # ... and the message is still there to be delivered once the wait is over.
    broadcast._pause_until = 0.0
    bot_db.mark_broadcast_row(int(row["id"]), "pending", next_attempt_at="")
    _ok(broadcast.drain_once(bot) == "sent")
    _ok(bot.sent == [(10, "hi")], bot.sent)


def test_blocked_user_is_flagged_not_retried() -> None:
    _reset()
    _seed_users(20)
    broadcast.enqueue("b4", [20], body="hi")
    bot = FakeBot({20: _api_error(403, "Forbidden: bot was blocked by the user")})

    _ok(broadcast.drain_once(bot) == "blocked")
    _ok(_row("b4", 20)["state"] == "blocked")
    _ok(bot_db.next_broadcast_rows(10) == [], "a blocked row must never come back")
    with bot_db.db() as conn:
        flag = conn.execute(
            "SELECT blocked_bot FROM users WHERE user_id = 20"
        ).fetchone()[0]
    _ok(int(flag) == 1, "user must be flagged as having blocked the bot")
    # And that flag keeps them out of later audiences.
    _ok(20 not in bot_db.list_all_user_ids(exclude_blocked=True))


def test_bad_request_fails_without_retry() -> None:
    _reset()
    _seed_users(30)
    broadcast.enqueue("b5", [30], body="<b>broken")
    bot = FakeBot({30: _api_error(400, "Bad Request: can't parse entities")})
    _ok(broadcast.drain_once(bot) == "failed")
    _ok(_row("b5", 30)["state"] == "failed")


def test_network_error_retries_then_fails() -> None:
    _reset()
    _seed_users(40)
    broadcast.enqueue("b6", [40], body="hi")

    bot = FakeBot({40: RuntimeError("connection reset")})
    _ok(broadcast.drain_once(bot) == "pending", "a network blip must be retried")
    row = _row("b6", 40)
    _ok(int(row["attempts"]) == 1, row)

    # Exhaust the attempt budget: the row must end as failed, not loop forever.
    for _ in range(broadcast.MAX_ATTEMPTS):
        row = _row("b6", 40)
        if row["state"] != "pending":
            break
        bot_db.mark_broadcast_row(int(row["id"]), "pending", attempts=int(row["attempts"]), next_attempt_at="")
        bot.fail_with[40] = RuntimeError("connection reset")
        broadcast.drain_once(bot)
    _ok(_row("b6", 40)["state"] == "failed", _row("b6", 40))


def test_tier_order_and_drip() -> None:
    _reset()
    # VIP is admin-granted (bot/limits.py:is_vip), not a paid plan. Granted
    # through the database rather than by poking `config` in this module's
    # namespace: several test modules re-import `bot.*`, and after that the
    # `config` object here is a different instance from the one `bot.limits`
    # reads — the mutation would land on nobody and VIP would rank as free.
    # Resolved from `sys.modules` at call time, not captured at import: after
    # another test module re-imports `bot.*`, this file's bindings can point at
    # an older generation, and `broadcast.tier_rank` looks the tier up in the
    # live one. Granting on a stale module grants to nobody, and `tier_rank`
    # swallows the miss into the free rank.
    from bot import db as live_db
    from bot import limits as live_limits

    vip_id, free_id = 101, 102
    _seed_users(vip_id, free_id)
    live_db.set_user_limit_override(vip_id, 1000, admin_id=1)
    live_limits.invalidate(vip_id)

    broadcast.enqueue("b7", [free_id, vip_id], body="hi", interval_sec=10.0)
    ranks = {r["user_id"]: r["rank"] for r in bot_db.next_broadcast_rows(10)}
    _ok(ranks[vip_id] < ranks[free_id], f"VIP must outrank free: {ranks}")

    bot = FakeBot()
    _ok(broadcast.drain_once(bot) == "sent")
    _ok(bot.sent[0][0] == vip_id, f"VIP must be served first, got {bot.sent}")
    # Second row of the same batch waits out the interval.
    _ok(broadcast.drain_once(bot) == "cooldown", "10s drip must hold the next row")
    _ok(len(bot.sent) == 1, bot.sent)

    live_db.del_user_limit_override(vip_id)
    live_limits.invalidate(vip_id)


def test_fast_batch_is_not_blocked_by_a_dripping_one() -> None:
    _reset()
    _seed_users(201, 202)
    broadcast.enqueue("slow", [201], body="slow", interval_sec=600.0)
    bot = FakeBot()
    broadcast.drain_once(bot)  # sends the slow batch's only ready row
    broadcast.enqueue("slow", [202], body="slow", interval_sec=600.0)
    broadcast.enqueue("fast", [202], body="fast", interval_sec=0.0)
    _ok(broadcast.drain_once(bot) == "sent")
    _ok(bot.sent[-1][1] == "fast", f"fast batch must not wait: {bot.sent}")


def test_sent_rows_are_never_picked_again() -> None:
    _reset()
    _seed_users(50)
    broadcast.enqueue("b8", [50], body="hi")
    bot = FakeBot()
    _ok(broadcast.drain_once(bot) == "sent")
    _ok(broadcast.drain_once(bot) == "empty", "a delivered row must not resurface")
    _ok(len(bot.sent) == 1, bot.sent)
    stats = bot_db.broadcast_batch_stats("b8")
    _ok(stats["sent"] == 1 and stats["pending"] == 0, stats)


def test_cancel_drops_only_pending() -> None:
    _reset()
    _seed_users(60, 61)
    broadcast.enqueue("b9", [60, 61], body="hi")
    bot = FakeBot()
    broadcast.drain_once(bot)
    dropped = broadcast.cancel("b9")
    _ok(dropped == 1, f"cancel dropped {dropped}")
    stats = bot_db.broadcast_batch_stats("b9")
    _ok(stats["sent"] == 1 and stats["pending"] == 0, stats)


def main() -> None:
    tests = [
        test_enqueue_is_idempotent,
        test_duplicate_ids_collapse,
        test_flood_control_reschedules_and_pauses,
        test_blocked_user_is_flagged_not_retried,
        test_bad_request_fails_without_retry,
        test_network_error_retries_then_fails,
        test_tier_order_and_drip,
        test_fast_batch_is_not_blocked_by_a_dripping_one,
        test_sent_rows_are_never_picked_again,
        test_cancel_drops_only_pending,
    ]
    failed = 0
    for tfn in tests:
        try:
            tfn()
            print(f"PASS {tfn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {tfn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
