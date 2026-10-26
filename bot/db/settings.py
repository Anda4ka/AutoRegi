"""bot_settings kv, tier limit knobs, per-user overrides, markers, RT-off."""

from __future__ import annotations

import os
import sqlite3

from bot.config import FREE_OK_LIMIT
from bot.db import _invalidate_limits, _now, db, init_db
from bot.db.audit import log_admin_action


def get_setting(key: str, default: str = "") -> str:
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        init_db()
        with db() as conn:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (key,)
            ).fetchone()
    if row is None:
        return default
    return str(row["value"])


def set_setting(key: str, value: str, *, updated_by: int | None = None) -> None:
    now = _now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO bot_settings (key, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (key, str(value), now, updated_by),
        )
        conn.commit()


def get_free_limit() -> int:
    try:
        return max(0, int(get_setting("free_limit", str(FREE_OK_LIMIT))))
    except ValueError:
        return int(FREE_OK_LIMIT)


def set_free_limit(n: int, *, admin_id: int | None = None) -> tuple[bool, str]:
    # The old own_limit coupling is gone: own proxies grant no tier since
    # 2026-09-09, so free_limit no longer has to stay under a second knob.
    n = int(n)
    if n < 0:
        return False, "free_limit должен быть ≥ 0"
    set_setting("free_limit", str(n), updated_by=admin_id)
    _invalidate_limits()
    return True, f"free_limit = {n}"


def get_user_limit_override(user_id: int) -> int | None:
    with db() as conn:
        row = conn.execute(
            "SELECT ok_limit FROM user_limit_overrides WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if row is None:
            return None
        return int(row["ok_limit"])


def set_user_limit_override(
    user_id: int, ok_limit: int, *, admin_id: int | None = None
) -> tuple[bool, str]:
    n = int(ok_limit)
    if n < 1:
        return False, "VIP limit должен быть ≥ 1"
    now = _now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_limit_overrides (user_id, ok_limit, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                ok_limit = excluded.ok_limit,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (int(user_id), n, now, admin_id),
        )
        conn.commit()
    _invalidate_limits(int(user_id))
    return True, f"VIP {user_id} → limit {n}"


def del_user_limit_override(user_id: int) -> bool:
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM user_limit_overrides WHERE user_id = ?",
            (int(user_id),),
        )
        conn.commit()
        removed = cur.rowcount > 0
    _invalidate_limits(int(user_id))
    return removed


def list_user_limit_overrides() -> list[tuple[int, int]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, ok_limit FROM user_limit_overrides ORDER BY user_id"
        ).fetchall()
        return [(int(r["user_id"]), int(r["ok_limit"])) for r in rows]


def get_alert_marker(key: str) -> str:
    return get_setting(f"alert_{key}", "")


def set_alert_marker(key: str, value: str) -> None:
    set_setting(f"alert_{key}", value)


# --- Runtime project disable (SQLite; does not touch project.toml) ---

_RT_OFF_PREFIX = "project_rt_off:"


def is_project_runtime_disabled(project_id: str) -> bool:
    pid = (project_id or "").strip()
    if not pid:
        return False
    return get_setting(f"{_RT_OFF_PREFIX}{pid}", "") == "1"


def set_project_runtime_disabled(
    project_id: str,
    disabled: bool,
    *,
    admin_id: int | None = None,
) -> None:
    pid = (project_id or "").strip()
    if not pid:
        raise ValueError("empty project_id")
    set_setting(
        f"{_RT_OFF_PREFIX}{pid}",
        "1" if disabled else "0",
        updated_by=admin_id,
    )
    # Keep duplicate in-process module instances (created by hot reloads and
    # isolated test imports) in sync with the SQLite flag. Normal processes
    # still use SQLite as the source of truth.
    marker = "AUTOREG_RT_OFF_" + "".join(
        ch if ch.isalnum() else "_" for ch in pid.upper()
    )
    if disabled:
        os.environ[marker] = "1"
    else:
        os.environ.pop(marker, None)
    log_admin_action(
        admin_id or 0,
        "project_rt_off" if disabled else "project_rt_on",
        detail=pid,
    )


def list_runtime_disabled_project_ids() -> list[str]:
    """Project ids with runtime disable flag set to 1 (sorted)."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT key FROM bot_settings
            WHERE key LIKE ? AND value = '1'
            ORDER BY key
            """,
            (f"{_RT_OFF_PREFIX}%",),
        ).fetchall()
    out: list[str] = []
    for r in rows:
        key = str(r["key"] or "")
        if key.startswith(_RT_OFF_PREFIX):
            pid = key[len(_RT_OFF_PREFIX) :].strip()
            if pid:
                out.append(pid)
    return out
