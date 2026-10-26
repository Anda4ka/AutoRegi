"""Durable broadcast outbox (see bot/broadcast.py for the sender)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bot.db import _now, db


def enqueue_broadcast_rows(
    batch_id: str,
    recipients: list[dict],
    *,
    parse_mode: str = "",
    interval_sec: float = 0.0,
) -> int:
    """Queue rows ``{user_id, rank, body, markup_json}``. Returns rows inserted.

    Body and markup are per row because a batch is written in each recipient's
    own language; pacing and parse mode belong to the batch.

    ``INSERT OR IGNORE`` against the unique (batch_id, user_id) index: enqueuing
    the same batch twice cannot double-send.
    """
    now = _now()
    payload = [
        (
            str(batch_id),
            int(item["user_id"]),
            int(item.get("rank") or 0),
            str(item.get("body") or ""),
            str(parse_mode or ""),
            str(item.get("markup_json") or ""),
            float(interval_sec or 0.0),
            now,
        )
        for item in recipients
    ]
    if not payload:
        return 0
    with db() as conn:
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO broadcast_outbox
                (batch_id, user_id, rank, body, parse_mode, markup_json,
                 interval_sec, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        conn.commit()
        return int(cur.rowcount or 0)


def next_broadcast_rows(limit: int = 50, *, now_iso: str | None = None) -> list[dict]:
    """Ready pending rows, best first: higher tier (lower rank), then FIFO."""
    now = now_iso or _now()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM broadcast_outbox
            WHERE state = 'pending'
              AND (next_attempt_at = '' OR next_attempt_at <= ?)
            ORDER BY rank ASC, id ASC
            LIMIT ?
            """,
            (now, max(1, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_broadcast_row(
    row_id: int,
    state: str,
    *,
    error: str = "",
    attempts: int | None = None,
    next_attempt_at: str = "",
) -> None:
    sets = ["state = ?", "error = ?", "next_attempt_at = ?"]
    args: list[Any] = [str(state), str(error or "")[:500], str(next_attempt_at or "")]
    if attempts is not None:
        sets.append("attempts = ?")
        args.append(int(attempts))
    if state == "sent":
        sets.append("sent_at = ?")
        args.append(_now())
    args.append(int(row_id))
    with db() as conn:
        conn.execute(
            f"UPDATE broadcast_outbox SET {', '.join(sets)} WHERE id = ?", args
        )
        conn.commit()


def broadcast_batch_stats(batch_id: str) -> dict[str, int]:
    with db() as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM broadcast_outbox "
            "WHERE batch_id = ? GROUP BY state",
            (str(batch_id),),
        ).fetchall()
    stats = {"pending": 0, "sent": 0, "failed": 0, "blocked": 0}
    for row in rows:
        stats[str(row["state"])] = int(row["n"])
    stats["total"] = sum(
        v for k, v in stats.items() if k in ("pending", "sent", "failed", "blocked")
    )
    return stats


def cancel_broadcast_batch(batch_id: str) -> int:
    """Drop what has not gone out yet. Already-sent rows stay for the report."""
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM broadcast_outbox WHERE batch_id = ? AND state = 'pending'",
            (str(batch_id),),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def list_pending_broadcast_batches() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT batch_id FROM broadcast_outbox WHERE state = 'pending'"
        ).fetchall()
        return [str(r["batch_id"]) for r in rows]


def purge_broadcast_rows(days: int = 14) -> int:
    """Housekeeping: drop finished rows older than ``days``."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM broadcast_outbox WHERE state != 'pending' AND created_at < ?",
            (cutoff,),
        )
        conn.commit()
        return int(cur.rowcount or 0)
