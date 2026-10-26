"""Paid plans: subscriptions and the NOWPayments ledger (money is TEXT)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.db import (
    _fetchone_healing,
    _invalidate_limits,
    _now,
    db,
    parse_iso,
)
from bot.db.audit import log_admin_action


def get_subscription(user_id: int) -> dict | None:
    row = _fetchone_healing(
        "SELECT * FROM user_subscriptions WHERE user_id = ?", (int(user_id),)
    )
    return dict(row) if row else None


def upsert_subscription(
    user_id: int,
    plan: str,
    expires_at: str,
    *,
    source: str = "",
    started_at: str | None = None,
    updated_by: int | None = None,
) -> None:
    uid = int(user_id)
    now = _now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_subscriptions
                (user_id, plan, expires_at, source, started_at, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                plan       = excluded.plan,
                expires_at = excluded.expires_at,
                source     = excluded.source,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (uid, str(plan), str(expires_at), str(source), started_at or now, now, updated_by),
        )
        conn.commit()
    _invalidate_limits(uid)


def delete_subscription(user_id: int, *, admin_id: int | None = None) -> bool:
    uid = int(user_id)
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM user_subscriptions WHERE user_id = ?", (uid,)
        )
        conn.commit()
        removed = cur.rowcount > 0
    if removed:
        _invalidate_limits(uid)
        if admin_id is not None:
            log_admin_action(admin_id, "plan_revoke", target_user_id=uid)
    return removed


def list_active_subscriptions(
    *, now_iso: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    now = now_iso or _now()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM user_subscriptions
            WHERE expires_at > ?
            ORDER BY expires_at ASC
            LIMIT ? OFFSET ?
            """,
            (now, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
    return [dict(r) for r in rows]


def count_active_subscriptions(*, now_iso: str | None = None) -> dict[str, int]:
    """Active subscriptions per plan id plus a ``total`` key."""
    now = now_iso or _now()
    out: dict[str, int] = {"total": 0}
    with db() as conn:
        rows = conn.execute(
            """
            SELECT plan, COUNT(*) AS n FROM user_subscriptions
            WHERE expires_at > ?
            GROUP BY plan
            """,
            (now,),
        ).fetchall()
    for row in rows:
        n = int(row["n"] or 0)
        out[str(row["plan"])] = n
        out["total"] += n
    return out


def subscriptions_expiring_between(start_iso: str, end_iso: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM user_subscriptions
            WHERE expires_at > ? AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (str(start_iso), str(end_iso)),
        ).fetchall()
    return [dict(r) for r in rows]


def create_payment_record(
    *,
    payment_id: str,
    order_id: str,
    user_id: int,
    plan: str,
    months: int = 1,
    price_amount: str,
    price_currency: str = "usd",
    pay_currency: str = "usdtbsc",
    pay_address: str = "",
    pay_amount: str = "",
    status: str = "waiting",
    valid_until: str = "",
) -> bool:
    now = _now()
    with db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO payments
                    (payment_id, order_id, user_id, plan, months, price_amount,
                     price_currency, pay_currency, pay_address, pay_amount,
                     status, valid_until, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payment_id),
                    str(order_id),
                    int(user_id),
                    str(plan),
                    max(1, int(months)),
                    str(price_amount),
                    str(price_currency),
                    str(pay_currency),
                    str(pay_address),
                    str(pay_amount),
                    str(status),
                    str(valid_until),
                    now,
                    now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return False
    return True


def get_payment_record(payment_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE payment_id = ?", (str(payment_id),)
        ).fetchone()
    return dict(row) if row else None


def get_open_payment_for_user(user_id: int) -> dict | None:
    """Newest payment still worth showing (not credited, not terminal)."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM payments
            WHERE user_id = ?
              AND credited_at IS NULL
              AND status NOT IN ('finished', 'failed', 'refunded', 'expired')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
    return dict(row) if row else None


def list_pollable_payments(*, cutoff_iso: str, limit: int = 25) -> list[dict]:
    """Open payments created after ``cutoff_iso``, oldest poll first (fair)."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM payments
            WHERE credited_at IS NULL
              AND status NOT IN ('finished', 'failed', 'refunded', 'expired')
              AND created_at > ?
            ORDER BY last_poll_at ASC, created_at ASC
            LIMIT ?
            """,
            (str(cutoff_iso), max(1, int(limit))),
        ).fetchall()
    return [dict(r) for r in rows]


def update_payment_status(
    payment_id: str,
    status: str,
    *,
    actually_paid: str | None = None,
    detail: str = "",
    mark_polled: bool = True,
) -> None:
    now = _now()
    sets = ["status = ?", "updated_at = ?"]
    args: list[Any] = [str(status), now]
    if actually_paid is not None:
        sets.append("actually_paid = ?")
        args.append(str(actually_paid))
    if detail:
        sets.append("detail = ?")
        args.append(str(detail)[:500])
    if mark_polled:
        sets.append("last_poll_at = ?")
        args.append(now)
        sets.append("poll_attempts = poll_attempts + 1")
    args.append(str(payment_id))
    with db() as conn:
        conn.execute(
            f"UPDATE payments SET {', '.join(sets)} WHERE payment_id = ?", args
        )
        conn.commit()


def set_payment_message(payment_id: str, chat_id: int, message_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE payments SET chat_id = ?, message_id = ?, updated_at = ? "
            "WHERE payment_id = ?",
            (int(chat_id), int(message_id), _now(), str(payment_id)),
        )
        conn.commit()


def _plan_value(plan_id: str) -> tuple[int, float]:
    """(rank, monthly price) for a plan; falls back to a neutral value."""
    try:
        from bot import billing

        plan = billing.get_plan(plan_id)
        return plan.rank, float(plan.price_usd)
    except Exception:  # pragma: no cover - billing must never break crediting
        return 1, 1.0


def credit_payment_if_new(
    payment_id: str, *, period_days: int = 30
) -> tuple[bool, str]:
    """Mark a payment credited and extend the subscription — exactly once.

    The ``credited_at IS NULL`` guard is the whole idempotency story: a repeated
    ``finished`` (poller restart, duplicate row in a batch, two threads) updates
    zero rows and returns ``(False, "already_credited")``. Nothing else in the
    codebase may grant a paid plan.
    """
    pid = str(payment_id)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE payments
               SET status = 'finished', credited_at = ?, updated_at = ?
             WHERE payment_id = ? AND credited_at IS NULL
            """,
            (now, now, pid),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False, "already_credited"
        row = conn.execute(
            "SELECT user_id, plan, months FROM payments WHERE payment_id = ?",
            (pid,),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the UPDATE above
            conn.rollback()
            return False, "not_found"
        uid = int(row["user_id"])
        bought_plan = str(row["plan"])
        months = max(1, int(row["months"] or 1))

        sub = conn.execute(
            "SELECT plan, expires_at, started_at FROM user_subscriptions WHERE user_id = ?",
            (uid,),
        ).fetchone()

        bought_rank, bought_price = _plan_value(bought_plan)
        bought_days = max(1, int(period_days)) * months
        target_plan, target_price = bought_plan, bought_price
        carried_days = 0.0

        if sub is not None:
            cur_expires = parse_iso(sub["expires_at"])
            cur_plan = str(sub["plan"])
            cur_rank, cur_price = _plan_value(cur_plan)
            days_left = 0.0
            if cur_expires is not None and cur_expires > now_dt:
                days_left = (cur_expires - now_dt).total_seconds() / 86400.0
            if cur_rank > bought_rank:
                target_plan, target_price = cur_plan, cur_price
            # Unused time is carried over at its cash value, so an upgrade
            # never silently burns days and a repeat purchase just stacks.
            carried_days = days_left * cur_price

        total_days = (bought_days * bought_price + carried_days) / max(target_price, 0.01)
        new_expires = (now_dt + timedelta(days=total_days)).isoformat()
        started_at = str(sub["started_at"]) if sub is not None else now

        conn.execute(
            """
            INSERT INTO user_subscriptions
                (user_id, plan, expires_at, source, started_at, updated_at, updated_by)
            VALUES (?, ?, ?, 'nowpayments', ?, ?, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                plan       = excluded.plan,
                expires_at = excluded.expires_at,
                source     = excluded.source,
                updated_at = excluded.updated_at
            """,
            (uid, target_plan, new_expires, started_at, now),
        )
        conn.execute(
            """
            INSERT INTO admin_audit (admin_id, action, target_user_id, detail, created_at)
            VALUES (0, 'plan_credit', ?, ?, ?)
            """,
            (uid, f"{pid} {target_plan} until {new_expires}"[:500], now),
        )
        conn.commit()
    _invalidate_limits(uid)
    return True, new_expires


def revenue_stats(*, days: int = 30) -> dict:
    """Credited payment totals: all time and the trailing ``days`` window."""
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
    out: dict[str, Any] = {"days": int(days), "by_plan": {}, "total": 0.0, "window": 0.0,
                           "count": 0, "window_count": 0}
    with db() as conn:
        rows = conn.execute(
            """
            SELECT plan, price_amount, credited_at FROM payments
            WHERE credited_at IS NOT NULL
            """
        ).fetchall()
    for row in rows:
        try:
            amount = float(row["price_amount"] or 0)
        except (TypeError, ValueError):
            amount = 0.0
        plan = str(row["plan"])
        out["by_plan"][plan] = round(out["by_plan"].get(plan, 0.0) + amount, 2)
        out["total"] = round(out["total"] + amount, 2)
        out["count"] += 1
        if str(row["credited_at"] or "") >= since:
            out["window"] = round(out["window"] + amount, 2)
            out["window_count"] += 1
    return out
