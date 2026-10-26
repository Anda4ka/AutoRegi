"""Read-only view of one user's queue, for the mini app.

Everything here opens the bot's SQLite **read-only** (`mode=ro`). The prototype
must not be able to change a job, spend a slot or corrupt the bot's state — it
is a second window onto the same data, not a second writer. Adding a write path
later means adding it deliberately, not discovering it was already possible.

Only ever called with a user id that came out of `webapp.auth`, so no function
here takes a "which user" hint from anywhere else.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Terms the bot already uses on the queue screen, so the two do not drift.
STATUS_RU = {
    "queued": "в очереди",
    "running": "идёт",
    "done": "готово",
    "cancelled": "отменено",
    "error": "ошибка",
}
ACTIVE = ("queued", "running")


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Read-only connection, closed when the block ends.

    ``with sqlite3.connect(...) as conn`` ends a transaction; it does not close
    anything. This is the hottest path in the process — the screen polls it
    every four seconds per open app — so leaving the descriptor to the cyclic
    collector is what turned a read into a slow leak. See ``bot.db.db``.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _job_row(row: sqlite3.Row, names: dict[str, str]) -> dict[str, Any]:
    total = int(row["total"] or 0)
    done = int(row["done"] or 0)
    return {
        "id": int(row["id"]),
        "project_id": row["project_id"],
        # Display name where there is one. A project removed from the registry
        # has no name left anywhere, so its id is all that can honestly be
        # shown — the row is flagged instead of hidden, because the successes
        # it holds are still the user's.
        "project": names.get(row["project_id"], row["project_id"]),
        "status": row["status"],
        "status_ru": STATUS_RU.get(row["status"], row["status"]),
        "total": total,
        "done": done,
        "percent": round(100 * done / total) if total else 0,
        "ok": int(row["ok"] or 0),
        "already": int(row["already"] or 0),
        "bad": int(row["bad"] or 0),
        "fail": int(row["fail"] or 0),
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def build_state(
    db_path: str,
    user_id: int,
    project_names: dict[str, str],
    *,
    history: int = 15,
    totals_limit: int = 12,
) -> dict[str, Any]:
    """Queue, recent jobs and per-project totals for exactly this user."""
    uid = int(user_id)
    with _connect(db_path) as conn:
        user = conn.execute(
            "SELECT first_name, username, lang, banned FROM users WHERE user_id = ?", (uid,)
        ).fetchone()

        active = [
            _job_row(r, project_names)
            for r in conn.execute(
                "SELECT * FROM submit_jobs WHERE user_id = ? AND status IN (?, ?)"
                " ORDER BY id",
                (uid, *ACTIVE),
            )
        ]
        recent = [
            _job_row(r, project_names)
            for r in conn.execute(
                "SELECT * FROM submit_jobs WHERE user_id = ? AND status NOT IN (?, ?)"
                " ORDER BY id DESC LIMIT ?",
                (uid, *ACTIVE, int(history)),
            )
        ]
        all_totals = [
            {
                "project_id": r["project_id"],
                "project": project_names.get(r["project_id"], r["project_id"]),
                "ok_count": int(r["ok_count"] or 0),
                "live": r["project_id"] in project_names,
            }
            for r in conn.execute(
                "SELECT project_id, ok_count FROM user_project_stats"
                " WHERE user_id = ? AND ok_count > 0 ORDER BY ok_count DESC",
                (uid,),
            )
        ]

    # A long-running account carries rows for dozens of projects, most of them
    # long removed. Live ones first, then the rest, and the tail is counted
    # rather than dropped — the total below still adds up to everything.
    all_totals.sort(key=lambda t: (not t["live"], -t["ok_count"]))
    totals = all_totals[: max(0, int(totals_limit))]

    return {
        "user": {
            "id": uid,
            "name": (user["first_name"] if user else "") or "",
            "username": (user["username"] if user else "") or "",
            "banned": bool(user["banned"]) if user else False,
        },
        "active": active,
        "recent": recent,
        "totals": totals,
        "totals_hidden": max(0, len(all_totals) - len(totals)),
        "summary": {
            "active_jobs": len(active),
            "rows_in_flight": sum(j["total"] - j["done"] for j in active),
            # Every project counts here, including the ones trimmed from the
            # list — the headline number must not shrink with the display.
            "ok_total": sum(t["ok_count"] for t in all_totals),
        },
    }
