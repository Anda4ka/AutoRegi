"""Admin audit log and the admin panel stats snapshot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bot.db import _now, db


def log_admin_action(
    admin_id: int,
    action: str,
    *,
    target_user_id: int | None = None,
    detail: str = "",
) -> None:
    now = _now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO admin_audit
                (admin_id, action, target_user_id, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(admin_id),
                (action or "")[:64],
                int(target_user_id) if target_user_id is not None else None,
                (detail or "")[:500],
                now,
            ),
        )
        conn.commit()


def admin_stats_snapshot(*, hours_24: bool = True) -> dict[str, Any]:
    """Aggregate stats for admin panel."""
    now = datetime.now(timezone.utc)
    since = (now.replace(microsecond=0) - timedelta(hours=24)).isoformat()
    with db() as conn:
        users_total = int(
            conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        )
        users_24 = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?",
                (since,),
            ).fetchone()["c"]
        )
        by_status_all = {
            str(r["status"]): int(r["c"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM submissions GROUP BY status"
            )
        }
        by_status_24 = {
            str(r["status"]): int(r["c"])
            for r in conn.execute(
                """
                SELECT status, COUNT(*) AS c FROM submissions
                WHERE created_at >= ? GROUP BY status
                """,
                (since,),
            )
        }
        ok_by_project = [
            (str(r["project_id"]), int(r["c"]))
            for r in conn.execute(
                """
                SELECT project_id, COUNT(*) AS c FROM submissions
                WHERE status = 'OK' GROUP BY project_id ORDER BY c DESC
                """
            )
        ]
        ok_users = int(
            conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS c FROM submissions WHERE status='OK'"
            ).fetchone()["c"]
        )
        banned_n = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE banned != 0"
            ).fetchone()["c"]
        )
        selected_n = int(
            conn.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE selected_project IS NOT NULL AND selected_project != ''
                """
            ).fetchone()["c"]
        )
        any_submit_users = int(
            conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS c FROM submissions"
            ).fetchone()["c"]
        )
    return {
        "users_total": users_total,
        "users_24h": users_24,
        "by_status_all": by_status_all,
        "by_status_24h": by_status_24,
        "ok_by_project": ok_by_project,
        "ok_users": ok_users,
        "banned_n": banned_n,
        "funnel": {
            "started": users_total,
            "selected": selected_n,
            "submitted": any_submit_users,
            "ok_users": ok_users,
        },
        "since_24h": since,
    }
