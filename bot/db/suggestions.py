"""Project suggestions (one per user per UTC day) and suggest pipeline runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from bot.db import _now, db


def utc_day_today() -> str:
    """YYYY-MM-DD in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def unprocessed_suggestions(limit: int = 5) -> list[dict]:
    """Suggestions with no pipeline run yet — the suggest pipeline's inbox."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.user_id, s.username, s.first_name, s.body, s.created_at
            FROM project_suggestions s
            LEFT JOIN suggest_pipeline_runs r ON r.suggestion_id = s.id
            WHERE r.id IS NULL
            ORDER BY s.id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    keys = ("id", "user_id", "username", "first_name", "body", "created_at")
    return [dict(zip(keys, r)) for r in rows]


def upsert_pipeline_run(
    suggestion_id: int,
    url: str,
    *,
    project_id: str = "",
    status: str = "",
    report: str = "",
    evidence: str = "",
) -> None:
    now = _now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO suggest_pipeline_runs
                (suggestion_id, url, project_id, status, report, evidence,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(suggestion_id, url) DO UPDATE SET
                project_id = excluded.project_id,
                status = excluded.status,
                report = excluded.report,
                evidence = excluded.evidence,
                updated_at = excluded.updated_at
            """,
            (suggestion_id, url, project_id, status, report, evidence, now, now),
        )


def has_suggestion_today(user_id: int, utc_day: str | None = None) -> bool:
    day = utc_day or utc_day_today()
    with db() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM project_suggestions
            WHERE user_id = ? AND utc_day = ?
            LIMIT 1
            """,
            (user_id, day),
        ).fetchone()
        return row is not None


def add_suggestion(
    user_id: int,
    username: str | None,
    first_name: str | None,
    body: str,
) -> dict:
    """
    Insert suggestion if user has no row for today's UTC day.
    Returns {"ok": True, "id": int, "utc_day": str} or
            {"ok": False, "reason": "limit", "utc_day": str}.
    """
    day = utc_day_today()
    now = _now()
    body = (body or "").strip()
    with db() as conn:
        exists = conn.execute(
            """
            SELECT id FROM project_suggestions
            WHERE user_id = ? AND utc_day = ?
            LIMIT 1
            """,
            (user_id, day),
        ).fetchone()
        if exists is not None:
            return {"ok": False, "reason": "limit", "utc_day": day}
        try:
            cur = conn.execute(
                """
                INSERT INTO project_suggestions
                    (user_id, username, first_name, body, created_at, utc_day)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, first_name, body, now, day),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            # Two different failures used to share this answer. The unique key
            # on (user_id, utc_day) is the race this was written for; a foreign
            # key onto `users` is somebody the bot has never seen, and telling
            # them "one per day" for their first ever message sends them off to
            # wait a day for nothing.
            if "foreign key" in str(exc).lower():
                return {"ok": False, "reason": "unknown_user", "utc_day": day}
            return {"ok": False, "reason": "limit", "utc_day": day}
        return {
            "ok": True,
            "id": int(cur.lastrowid),
            "utc_day": day,
            "created_at": now,
        }
