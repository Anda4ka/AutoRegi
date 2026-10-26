"""User identity, language, anchored screen, ban and audience lists."""

from __future__ import annotations

from bot.db import _fetchone_healing, _now, db, init_db
from bot.db.audit import log_admin_action


def is_known_user(user_id: int) -> bool:
    """True if user row already exists (used for first-visit detection)."""
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return row is not None


def ensure_user(user_id: int, username: str | None, first_name: str | None) -> dict:
    """Upsert user. Safe under concurrent handlers (no UNIQUE race on insert)."""
    init_db()
    now = _now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users
                (user_id, username, first_name, ok_count, selected_project,
                 created_at, updated_at)
            VALUES (?, ?, ?, 0, NULL, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at
            """,
            (user_id, username, first_name, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row)


def get_selected_project(user_id: int) -> str | None:
    with db() as conn:
        row = conn.execute(
            "SELECT selected_project FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        val = row["selected_project"]
        return str(val) if val else None


def set_selected_project(user_id: int, project_id: str | None) -> None:
    now = _now()
    with db() as conn:
        conn.execute(
            """
            UPDATE users SET selected_project = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (project_id, now, user_id),
        )
        conn.commit()


def get_auto_reg(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT auto_reg FROM users WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return bool(row and int(row["auto_reg"] or 0))


def set_auto_reg(user_id: int, on: bool) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET auto_reg = ?, updated_at = ? WHERE user_id = ?",
            (1 if on else 0, _now(), int(user_id)),
        )
        conn.commit()


def auto_reg_user_ids() -> list[int]:
    """Opted-in, known, non-banned users — the auto-registration audience."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id FROM users
            WHERE auto_reg = 1
              AND (banned = 0 OR banned IS NULL)
              AND (blocked_bot = 0 OR blocked_bot IS NULL)
            """
        ).fetchall()
    return [int(r["user_id"]) for r in rows]


def get_mint_remind(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT mint_remind FROM users WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return bool(row and int(row["mint_remind"] or 0))


def set_mint_remind(user_id: int, on: bool) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET mint_remind = ?, updated_at = ? WHERE user_id = ?",
            (1 if on else 0, _now(), int(user_id)),
        )
        conn.commit()


def mint_remind_user_ids() -> list[int]:
    """Opted-in, non-banned, not-blocked — the T-1h reminder audience."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id FROM users
            WHERE mint_remind = 1
              AND (banned = 0 OR banned IS NULL)
              AND (blocked_bot = 0 OR blocked_bot IS NULL)
            """
        ).fetchall()
    return [int(r["user_id"]) for r in rows]


def get_lang(user_id: int) -> str:
    """Return user language code ('ru' | 'en'). Default ru."""
    with db() as conn:
        row = conn.execute(
            "SELECT lang FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return "ru"
        try:
            val = row["lang"]
        except (KeyError, IndexError):
            return "ru"
        v = (val or "ru").strip().lower()
        return "en" if v.startswith("en") else "ru"


def set_lang(user_id: int, lang: str) -> None:
    now = _now()
    code = "en" if (lang or "").strip().lower().startswith("en") else "ru"
    with db() as conn:
        conn.execute(
            """
            UPDATE users SET lang = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (code, now, user_id),
        )
        conn.commit()


def get_screen(user_id: int) -> dict | None:
    """Anchored live-screen pointer for this user (see bot/screen.py)."""
    row = _fetchone_healing(
        """
        SELECT screen_chat_id, screen_message_id, screen_state,
               screen_params, screen_updated_at
          FROM users WHERE user_id = ?
        """,
        (int(user_id),),
    )
    if row is None or row["screen_message_id"] is None:
        return None
    return {
        "chat_id": int(row["screen_chat_id"] or 0),
        "message_id": int(row["screen_message_id"]),
        "state": str(row["screen_state"] or ""),
        "params": str(row["screen_params"] or ""),
        "updated_at": str(row["screen_updated_at"] or ""),
    }


def set_screen(
    user_id: int,
    chat_id: int,
    message_id: int,
    state: str,
    params_json: str = "",
) -> None:
    if not is_known_user(user_id):
        # A background sender (payment poller) may reach a user the handlers
        # have not touched yet; without a row the UPDATE below is a silent no-op.
        ensure_user(int(user_id), None, None)
    with db() as conn:
        conn.execute(
            """
            UPDATE users
               SET screen_chat_id = ?, screen_message_id = ?, screen_state = ?,
                   screen_params = ?, screen_updated_at = ?
             WHERE user_id = ?
            """,
            (
                int(chat_id),
                int(message_id),
                str(state),
                str(params_json)[:2000],
                _now(),
                int(user_id),
            ),
        )
        conn.commit()


def clear_screen(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE users
               SET screen_chat_id = NULL, screen_message_id = NULL,
                   screen_state = '', screen_params = '', screen_updated_at = ''
             WHERE user_id = ?
            """,
            (int(user_id),),
        )
        conn.commit()


# --- ban / audit / admin stats ---


def is_banned(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT banned FROM users WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            return False
        return int(row["banned"] or 0) != 0


def set_banned(
    user_id: int,
    banned: bool,
    *,
    admin_id: int | None = None,
    reason: str = "",
) -> None:
    now = _now()
    with db() as conn:
        # ensure row exists
        conn.execute(
            """
            INSERT INTO users
                (user_id, username, first_name, ok_count, selected_project,
                 created_at, updated_at, banned, banned_at, ban_reason)
            VALUES (?, NULL, NULL, 0, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                banned = excluded.banned,
                banned_at = excluded.banned_at,
                ban_reason = excluded.ban_reason,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                now,
                now,
                1 if banned else 0,
                now if banned else None,
                (reason or "")[:200] if banned else None,
            ),
        )
        conn.commit()
    log_admin_action(
        admin_id or 0,
        "ban" if banned else "unban",
        target_user_id=int(user_id),
        detail=reason or "",
    )


def find_user_id(query: str) -> int | None:
    """Resolve user_id from digits or @username."""
    q = (query or "").strip()
    if not q:
        return None
    q = q.removeprefix("@")
    if q.isdigit():
        return int(q)
    with db() as conn:
        row = conn.execute(
            """
            SELECT user_id FROM users
            WHERE lower(username) = lower(?)
            ORDER BY updated_at DESC LIMIT 1
            """,
            (q,),
        ).fetchone()
        return int(row["user_id"]) if row else None


def get_user_row(user_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return dict(row) if row else None


def list_all_user_ids(
    *, exclude_banned: bool = True, exclude_blocked: bool = False
) -> list[int]:
    where: list[str] = []
    if exclude_banned:
        where.append("(banned = 0 OR banned IS NULL)")
    if exclude_blocked:
        where.append("(blocked_bot = 0 OR blocked_bot IS NULL)")
    sql = "SELECT user_id FROM users"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with db() as conn:
        rows = conn.execute(sql).fetchall()
        return [int(r["user_id"]) for r in rows]


def set_bot_blocked(user_id: int, blocked: bool = True) -> None:
    """Mark that Telegram refuses delivery to this user (403), or clear it."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET blocked_bot = ?, updated_at = ? WHERE user_id = ?",
            (1 if blocked else 0, _now(), int(user_id)),
        )
        conn.commit()
