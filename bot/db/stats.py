"""Submissions, per-project stats, wallet dedup/claims, OK-slot reservations."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import time

from bot.config import BOT_RESULTS
from bot.db import _now, db
from bot.db.audit import log_admin_action

_log = logging.getLogger(__name__)
_results_lock = threading.Lock()



def get_ok_count(user_id: int, project_id: str) -> int:
    with db() as conn:
        row = conn.execute(
            """
            SELECT ok_count FROM user_project_stats
            WHERE user_id = ? AND project_id = ?
            """,
            (user_id, project_id),
        ).fetchone()
        return int(row["ok_count"]) if row else 0


def get_all_ok_counts(user_id: int) -> dict[str, int]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT project_id, ok_count FROM user_project_stats
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
        return {str(r["project_id"]): int(r["ok_count"]) for r in rows}


def _is_evm_wallet(w: str) -> bool:
    """Exactly 40 hex chars after 0x — a 64-hex tx hash must not pass (16.3)."""
    return len(w) == 42 and w.startswith("0x") and all(c in "0123456789abcdef" for c in w[2:])


def already_registered_wallet(
    project_id: str,
    wallet: str | None,
    *,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    """
    Latest OK/ALREADY for project+wallet, or None.

    Default **global** (any Telegram user). Pass user_id=… for local-only
    match (same user). Skip HTTP / do not burn OK slot when found.
    """
    w = (wallet or "").strip().lower()
    if not _is_evm_wallet(w):
        return None
    pid = (project_id or "").strip()
    if not pid:
        return None
    with db() as conn:
        if user_id is not None:
            row = conn.execute(
                """
                SELECT user_id, status, handle, created_at, detail
                FROM submissions
                WHERE user_id = ?
                  AND project_id = ?
                  AND lower(wallet) = ?
                  AND status IN ('OK', 'ALREADY')
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(user_id), pid, w),
            ).fetchone()
            scope = "local"
        else:
            row = conn.execute(
                """
                SELECT user_id, status, handle, created_at, detail
                FROM submissions
                WHERE project_id = ?
                  AND lower(wallet) = ?
                  AND status IN ('OK', 'ALREADY')
                ORDER BY id DESC
                LIMIT 1
                """,
                (pid, w),
            ).fetchone()
            scope = "global"
        if row is None:
            return None
        return {
            "status": str(row["status"] or ""),
            "handle": str(row["handle"] or ""),
            "created_at": str(row["created_at"] or ""),
            "detail": str(row["detail"] or ""),
            "wallet": w,
            "by_user_id": int(row["user_id"]) if row["user_id"] is not None else None,
            "scope": scope,
        }


def try_claim_wallet(
    project_id: str,
    wallet: str | None,
    user_id: int,
    *,
    stale_seconds: int = 600,
) -> tuple[bool, str]:
    """
    Atomic in-flight claim for (project_id, wallet) before HTTP (AUD-007).

    Returns (ok_to_submit, reason):
      - (True, "") — claimed; caller must release_wallet_claim on FAIL
      - (False, "already") — submissions already OK/ALREADY
      - (False, "in_flight") — another worker holds claim
    """
    w = (wallet or "").strip().lower()
    pid = (project_id or "").strip()
    if not pid or not _is_evm_wallet(w):
        return True, ""  # no claimable wallet — skip claim

    now = _now()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prev = conn.execute(
            """
            SELECT 1 FROM submissions
            WHERE project_id = ? AND lower(wallet) = ?
              AND status IN ('OK', 'ALREADY')
            LIMIT 1
            """,
            (pid, w),
        ).fetchone()
        if prev is not None:
            conn.rollback()
            return False, "already"
        row = conn.execute(
            """
            SELECT user_id, created_at FROM wallet_claims
            WHERE project_id = ? AND wallet = ?
            """,
            (pid, w),
        ).fetchone()
        if row is not None:
            # Stale claim from crashed worker → steal
            created = str(row["created_at"] or "")
            try:
                from datetime import datetime, timezone

                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
            except Exception:
                # Unreadable timestamp: steal rather than park the row forever.
                age = float(stale_seconds)
            if age >= float(stale_seconds):
                conn.execute(
                    "DELETE FROM wallet_claims WHERE project_id = ? AND wallet = ?",
                    (pid, w),
                )
            else:
                conn.rollback()
                return False, "in_flight"
        try:
            conn.execute(
                """
                INSERT INTO wallet_claims (project_id, wallet, user_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (pid, w, int(user_id), now),
            )
            conn.commit()
            return True, ""
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "in_flight"


def release_wallet_claim(project_id: str, wallet: str | None) -> None:
    """Release claim after FAIL / transport error so retries can proceed."""
    w = (wallet or "").strip().lower()
    pid = (project_id or "").strip()
    if not pid or not w:
        return
    with db() as conn:
        conn.execute(
            "DELETE FROM wallet_claims WHERE project_id = ? AND wallet = ?",
            (pid, w),
        )
        conn.commit()


def release_wallet_claims(project_id: str, wallets: list[str]) -> int:
    """Release in-flight claims for a job being recovered after a restart.

    The bot is single-poller, so claims belonging to a recovered job cannot
    belong to another live submitter. Successful rows are still protected by
    the submissions table and global dedup before any HTTP request.
    """
    pid = (project_id or "").strip()
    norm = sorted({(w or "").strip().lower() for w in wallets if (w or "").strip()})
    if not pid or not norm:
        return 0
    removed = 0
    with db() as conn:
        for start in range(0, len(norm), 400):
            chunk = norm[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.execute(
                f"DELETE FROM wallet_claims WHERE project_id = ? "
                f"AND wallet IN ({placeholders})",
                (pid, *chunk),
            )
            removed += int(cur.rowcount or 0)
        conn.commit()
    return removed


def finalize_wallet_claim(project_id: str, wallet: str | None) -> None:
    """
    After OK/ALREADY the submissions row is source of truth — drop claim.
    (same as release; named for call-site clarity)
    """
    release_wallet_claim(project_id, wallet)


def try_reserve_ok_slot(
    user_id: int,
    project_id: str,
    ok_limit: int,
) -> str | None:
    """Atomically reserve one possible OK slot before an HTTP request.

    In-flight requests count against the limit. The returned opaque token must
    be released on an early exception or passed to
    :func:`record_submission_and_finalize_claim` for transactional cleanup.
    """
    uid = int(user_id)
    pid = (project_id or "").strip()
    limit = max(0, int(ok_limit))
    if not pid or limit <= 0:
        return None
    token = uuid.uuid4().hex
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM ok_slot_reservations WHERE created_at < ?",
            (cutoff,),
        )
        row = conn.execute(
            """
            SELECT ok_count FROM user_project_stats
            WHERE user_id = ? AND project_id = ?
            """,
            (uid, pid),
        ).fetchone()
        ok_count = int(row["ok_count"] or 0) if row else 0
        reserved = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM ok_slot_reservations
                WHERE user_id = ? AND project_id = ?
                """,
                (uid, pid),
            ).fetchone()[0]
        )
        if ok_count + reserved >= limit:
            conn.rollback()
            return None
        conn.execute(
            """
            INSERT INTO ok_slot_reservations
                (reservation_id, user_id, project_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, uid, pid, _now()),
        )
        conn.commit()
    return token


def release_ok_slot(reservation_id: str | None) -> None:
    token = (reservation_id or "").strip()
    if not token:
        return
    with db() as conn:
        conn.execute(
            "DELETE FROM ok_slot_reservations WHERE reservation_id = ?",
            (token,),
        )
        conn.commit()


def clear_ok_slot_reservations() -> int:
    """Clear process-local in-flight reservations during boot reconciliation."""
    with db() as conn:
        cur = conn.execute("DELETE FROM ok_slot_reservations")
        conn.commit()
        return int(cur.rowcount or 0)


def wallets_already_registered(
    project_id: str,
    wallets: list[str],
    *,
    user_id: int | None = None,
) -> set[str]:
    """
    Lowercased wallets that already have OK/ALREADY on this project.

    Default **global** (any user). Pass user_id= for local-only batch.
    """
    pid = (project_id or "").strip()
    if not pid or not wallets:
        return set()
    norm: list[str] = []
    seen: set[str] = set()
    for raw in wallets:
        w = (raw or "").strip().lower()
        if _is_evm_wallet(w) and w not in seen:
            seen.add(w)
            norm.append(w)
    if not norm:
        return set()
    out: set[str] = set()
    chunk = 400
    with db() as conn:
        for i in range(0, len(norm), chunk):
            part = norm[i : i + chunk]
            placeholders = ",".join("?" * len(part))
            if user_id is not None:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT lower(wallet) AS w
                    FROM submissions
                    WHERE user_id = ?
                      AND project_id = ?
                      AND status IN ('OK', 'ALREADY')
                      AND lower(wallet) IN ({placeholders})
                    """,
                    (int(user_id), pid, *part),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT lower(wallet) AS w
                    FROM submissions
                    WHERE project_id = ?
                      AND status IN ('OK', 'ALREADY')
                      AND lower(wallet) IN ({placeholders})
                    """,
                    (pid, *part),
                ).fetchall()
            for r in rows:
                ww = str(r["w"] or "").lower()
                if ww:
                    out.add(ww)
    return out


# Whose dedup answer is stale. A wallet that gets OK/ALREADY on a project is
# out of play for *everyone* on that project, so anything caching "how many of
# my wallets are still free here" has to notice. Per project rather than one
# global number: during a long run on one project, a global counter would
# invalidate every other project's answer on every row — the cache would be
# emptiest exactly when it is needed most.
_project_gen: dict[str, int] = {}
_project_gen_lock = threading.Lock()
# Which wallets caused the recent bumps, so a cache can ask "did any of *my*
# wallets move?" instead of throwing its answer away because somebody else's
# did. See `bot/fresh.py`; the trail is short on purpose — it is an
# optimisation, and falling off the end of it costs one recomputation.
_project_settled: dict[str, deque[tuple[int, str]]] = {}
_PROJECT_SETTLED_MAX = 512


def project_generation(project_id: str) -> int:
    """Bumped whenever a settled row lands on this project."""
    with _project_gen_lock:
        return _project_gen.get((project_id or "").strip(), 0)


def project_settled_since(project_id: str, since_generation: int) -> set[str] | None:
    """Wallets that settled on this project after ``since_generation``.

    ``None`` means "cannot say" — the caller asked about a generation older
    than the trail still reaches, so it has to recompute. That is the honest
    answer and the safe one: a partial list would let a caller conclude that
    nothing of theirs moved when something had.
    """
    pid = (project_id or "").strip()
    if not pid or since_generation <= 0:
        return None
    with _project_gen_lock:
        trail = _project_settled.get(pid)
        current = _project_gen.get(pid, 0)
        if trail is None:
            # No bump recorded since this process started. If the caller's
            # generation matches, nothing has moved and the empty set is exact.
            return set() if current == since_generation else None
        if not trail or trail[0][0] > since_generation + 1:
            return None  # the trail no longer covers the gap
        return {wallet for gen, wallet in trail if gen > since_generation}


def _bump_project_generation(project_id: str, wallet: str | None = None) -> None:
    pid = (project_id or "").strip()
    if not pid:
        return
    with _project_gen_lock:
        gen = _project_gen.get(pid, 0) + 1
        _project_gen[pid] = gen
        trail = _project_settled.get(pid)
        if trail is None:
            trail = _project_settled[pid] = deque(maxlen=_PROJECT_SETTLED_MAX)
        trail.append((gen, (wallet or "").strip().lower()))


def record_submission(
    user_id: int,
    project_id: str,
    handle: str | None,
    wallet: str | None,
    status: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> int:
    """Persist submission; bump per-project ok_count only on OK. Returns new ok_count."""
    return record_submission_and_finalize_claim(
        user_id,
        project_id,
        handle,
        wallet,
        status,
        detail,
        extra=extra,
    )


def record_submission_and_finalize_claim(
    user_id: int,
    project_id: str,
    handle: str | None,
    wallet: str | None,
    status: str,
    detail: str,
    extra: dict[str, Any] | None = None,
    *,
    claim_wallet: str | None = None,
    slot_reservation_id: str | None = None,
) -> int:
    """Persist a result and release its claim/reservation in one transaction."""
    if status in ("OK", "ALREADY"):
        _bump_project_generation(project_id, wallet)
    now = _now()
    extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO submissions
                (user_id, project_id, handle, wallet, extra_json, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                project_id,
                handle or "",
                wallet or "",
                extra_json,
                status,
                (detail or "")[:500],
                now,
            ),
        )
        if status == "OK":
            conn.execute(
                """
                INSERT INTO user_project_stats (user_id, project_id, ok_count, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id, project_id) DO UPDATE SET
                    ok_count = ok_count + 1,
                    updated_at = excluded.updated_at
                """,
                (user_id, project_id, now),
            )
            # Legacy users.ok_count sync for historical project_id only (module removed)
            if project_id == "legacy_seed":
                conn.execute(
                    """
                    UPDATE users SET ok_count = ok_count + 1, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now, user_id),
                )
        else:
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE user_id = ?",
                (now, user_id),
            )
        claim = (claim_wallet or "").strip().lower()
        if claim:
            conn.execute(
                "DELETE FROM wallet_claims WHERE project_id = ? AND wallet = ?",
                ((project_id or "").strip(), claim),
            )
        token = (slot_reservation_id or "").strip()
        if token:
            conn.execute(
                "DELETE FROM ok_slot_reservations WHERE reservation_id = ?",
                (token,),
            )
        conn.commit()
        row = conn.execute(
            """
            SELECT ok_count FROM user_project_stats
            WHERE user_id = ? AND project_id = ?
            """,
            (user_id, project_id),
        ).fetchone()
        return int(row["ok_count"]) if row else 0


def append_bot_result(row: dict) -> bool:
    """Best-effort JSONL audit mirror; SQLite remains authoritative."""
    path = Path(BOT_RESULTS)
    try:
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _results_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        return True
    except OSError as exc:
        _log.error("cannot append bot result to %s: %s", path, exc)
        return False


def reset_ok_count(
    user_id: int,
    project_id: str | None = None,
    *,
    admin_id: int | None = None,
) -> int:
    """Zero ok_count in user_project_stats. Does not delete submissions. Returns rows touched."""
    with db() as conn:
        if project_id:
            cur = conn.execute(
                """
                UPDATE user_project_stats SET ok_count = 0, updated_at = ?
                WHERE user_id = ? AND project_id = ?
                """,
                (_now(), int(user_id), project_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE user_project_stats SET ok_count = 0, updated_at = ?
                WHERE user_id = ?
                """,
                (_now(), int(user_id)),
            )
        # Legacy users.ok_count (was tied to a historical legacy project)
        if project_id is None or project_id == "legacy_seed":
            conn.execute(
                "UPDATE users SET ok_count = 0, updated_at = ? WHERE user_id = ?",
                (_now(), int(user_id)),
            )
        conn.commit()
        n = int(cur.rowcount or 0)
    log_admin_action(
        admin_id or 0,
        "reset_ok",
        target_user_id=int(user_id),
        detail=project_id or "all",
    )
    return n


def list_recent_submissions(user_id: int, limit: int = 5) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT project_id, status, detail, created_at, handle
            FROM submissions WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def project_result_mix_since(since_iso: str) -> dict[str, dict[str, int]]:
    """`{project_id: {status: count}}` for everything settled since `since_iso`.

    One indexed range scan (`idx_submissions_created`) and a group-by, because
    the caller runs it on a timer: a project whose route has started refusing
    every row must be noticed without asking the database for the rows.
    """
    out: dict[str, dict[str, int]] = {}
    with db() as conn:
        rows = conn.execute(
            """
            SELECT project_id, status, COUNT(*) AS c FROM submissions
            WHERE created_at >= ?
            GROUP BY project_id, status
            """,
            (since_iso,),
        ).fetchall()
    for row in rows:
        pid = (row["project_id"] if not isinstance(row, tuple) else row[0]) or ""
        status = (row["status"] if not isinstance(row, tuple) else row[1]) or ""
        count = int(row["c"] if not isinstance(row, tuple) else row[2])
        out.setdefault(pid, {})[status] = count
    return out


def top_failure_detail_since(
    project_id: str, since_iso: str, *, statuses: tuple[str, ...] = ("FAIL", "BAD_RESP")
) -> tuple[str, int, int]:
    """The most common failure text for one project: `(detail, its count, total)`.

    Asked only about a project that already looks stuck, so the scan is small
    and the answer says *what* the route is refusing with — which is the whole
    difference between «the site broke» and «our proxies broke».
    """
    marks = ",".join("?" for _ in statuses)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT detail, COUNT(*) AS c FROM submissions
            WHERE project_id = ? AND created_at >= ? AND status IN ({marks})
            GROUP BY detail
            ORDER BY c DESC
            LIMIT 1
            """,
            (project_id, since_iso, *statuses),
        ).fetchall()
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM submissions
            WHERE project_id = ? AND created_at >= ? AND status IN ({marks})
            """,
            (project_id, since_iso, *statuses),
        ).fetchone()
    if not rows:
        return "", 0, 0
    row = rows[0]
    detail = (row["detail"] if not isinstance(row, tuple) else row[0]) or ""
    count = int(row["c"] if not isinstance(row, tuple) else row[1])
    total = int((total_row["c"] if not isinstance(total_row, tuple) else total_row[0]) or 0)
    return detail, count, total


def count_submissions_since(
    status: str | None = None, *, since_iso: str
) -> int:
    with db() as conn:
        if status:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM submissions
                WHERE status = ? AND created_at >= ?
                """,
                (status, since_iso),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM submissions
                WHERE created_at >= ?
                """,
                (since_iso,),
            ).fetchone()
        return int(row["c"] if row else 0)


def list_recent_errors(limit: int = 20) -> list[dict]:
    """Last non-proxy errors for the admin panel.

    Proxy-transport FAILs (``is_proxy_fail_detail`` — dead/refusing/timing-out
    exits) and proxy block pages («HTTP 404: proxy block page») are routine:
    the pool benches those exits on its own and a slot is never spent, so they
    only bury the errors an admin can act on. They are filtered out here;
    everything else (BUSY, BAD_RESP, non-proxy FAIL) still shows.
    """
    from bot.http_client import is_proxy_block_page_detail, is_proxy_fail_detail

    limit = max(1, int(limit))
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, project_id, status, detail, created_at, handle
            FROM submissions
            WHERE status IN ('FAIL', 'BAD_RESP', 'BUSY')
            ORDER BY id DESC LIMIT ?
            """,
            (limit * 20,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        detail = str(r["detail"] or "")
        if is_proxy_fail_detail(detail) or is_proxy_block_page_detail(detail):
            continue
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def list_active_user_ids(
    *, days: int = 30, exclude_banned: bool = True, exclude_blocked: bool = True
) -> list[int]:
    """Users who submitted at least one row in the last ``days``.

    «Active» is deliberately measured by submissions rather than by any /start:
    the audience for a new-project alert is people who actually run rows, not
    everyone who once opened the bot.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
    where = ["s.created_at >= ?"]
    if exclude_banned:
        where.append("(u.banned = 0 OR u.banned IS NULL)")
    if exclude_blocked:
        where.append("(u.blocked_bot = 0 OR u.blocked_bot IS NULL)")
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT s.user_id FROM submissions s "
            "JOIN users u ON u.user_id = s.user_id "
            "WHERE " + " AND ".join(where),
            (since,),
        ).fetchall()
        return [int(r["user_id"]) for r in rows]


def user_project_registrations(
    user_id: int, project_id: str, *, limit: int = 5000
) -> list[dict]:
    """This user's OK/ALREADY rows for one project, newest first.

    The export feature («Мои регистрации»): what the user actually got on a
    project, straight off the same table the dedup reads. ``wallet`` is the
    stored casing; the dedup normalises, the export shows what was filed.
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT wallet, handle, status, created_at
            FROM submissions
            WHERE user_id = ? AND project_id = ?
              AND status IN ('OK', 'ALREADY')
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(user_id), (project_id or "").strip(), max(1, int(limit))),
        ).fetchall()
    return [dict(r) for r in rows]


def user_proxy_health(user_id: int, *, hours: int = 24) -> dict[str, int]:
    """Proxy-transport failures vs all settled rows for this user.

    ``fails_proxy`` counts FAIL rows whose detail classifies as a proxy
    transport error (`bot.http_client.is_proxy_fail_detail`) — the same
    signal the own-proxy dead alert uses, surfaced per user for the proxy
    screen and the mini app. FAIL rows with other details stay in ``fails``
    but do not implicate the proxies.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    ).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT status, detail FROM submissions
            WHERE user_id = ? AND created_at >= ?
            """,
            (int(user_id), since),
        ).fetchall()
    from bot.http_client import is_proxy_block_page_detail, is_proxy_fail_detail

    out = {"rows": 0, "fails": 0, "fails_proxy": 0, "ok": 0}
    for r in rows:
        out["rows"] += 1
        status = str(r["status"] or "")
        if status == "OK":
            out["ok"] += 1
        elif status == "FAIL":
            out["fails"] += 1
            detail = str(r["detail"] or "")
            if is_proxy_fail_detail(detail) or is_proxy_block_page_detail(detail):
                out["fails_proxy"] += 1
    return out


def user_stats_daily(user_id: int, *, days: int = 7) -> dict:
    """This user's own submission history, one bucket per UTC day.

    The queue tab shows "now"; this answers "and how did the week go" — the
    user's own rows only, statuses grouped the way the progress bar groups
    them. One indexed range scan on ``created_at`` (idx_submissions_created);
    the caller runs it on an explicit /api/stats tap, never on the state poll.
    """
    n_days = max(1, min(int(days), 90))
    since = (
        datetime.now(timezone.utc) - timedelta(days=n_days)
    ).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, project_id, status,
                   COUNT(*) AS c
            FROM submissions
            WHERE user_id = ? AND created_at >= ?
            GROUP BY day, project_id, status
            """,
            (int(user_id), since),
        ).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    by_project: dict[str, dict[str, int]] = {}
    totals = {"ok": 0, "already": 0, "bad": 0, "fail": 0}
    status_key = {
        "OK": "ok",
        "ALREADY": "already",
        "BAD_RESP": "bad",
        "FAIL": "fail",
    }
    for r in rows:
        day = str(r["day"] or "")
        pid = str(r["project_id"] or "")
        key = status_key.get(str(r["status"] or ""), "")
        n = int(r["c"] or 0)
        if not day or not key:
            continue
        bucket = by_day.setdefault(day, {"ok": 0, "already": 0, "bad": 0, "fail": 0})
        bucket[key] += n
        totals[key] += n
        if key == "ok":
            proj = by_project.setdefault(pid, {"ok": 0, "already": 0})
            proj["ok"] += n
        elif key == "already":
            proj = by_project.setdefault(pid, {"ok": 0, "already": 0})
            proj["already"] += n
    projects = [
        {"project_id": pid, "ok": v["ok"], "already": v["already"]}
        for pid, v in sorted(
            by_project.items(), key=lambda kv: (-kv[1]["ok"], kv[0])
        )
    ]
    return {"days": n_days, "by_day": by_day, "by_project": projects, "totals": totals}


def log_funnel_event(user_id: int, event: str, branch: str = "") -> None:
    """One приватка funnel ping. Failures must not break the caller."""
    name = (event or "").strip()
    if not name:
        return
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO funnel_events (user_id, event, branch, ts) "
                "VALUES (?, ?, ?, ?)",
                (int(user_id), name, str(branch or ""), int(time.time())),
            )
            conn.commit()
    except Exception:
        _log.exception("log_funnel_event %s", name)


def funnel_report(*, windows: tuple[int, ...] = (7, 30)) -> str:
    """Admin /funnel: counts, unique users, priv_view branches, win→menu."""
    now = int(time.time())
    lines = ["<b>Воронка приватки</b>"]
    with db() as conn:
        for days in windows:
            since = now - int(days) * 86400
            lines.append("")
            lines.append(f"<b>{days} дн.</b>")
            rows = conn.execute(
                "SELECT event, COUNT(*) c, COUNT(DISTINCT user_id) u "
                "FROM funnel_events WHERE ts >= ? "
                "GROUP BY event ORDER BY event",
                (since,),
            ).fetchall()
            if not rows:
                lines.append("• нет событий")
                continue
            for r in rows:
                lines.append(
                    f"• <code>{r['event']}</code>: {int(r['c'])} "
                    f"({int(r['u'])} чел.)"
                )
            branches = conn.execute(
                "SELECT branch, COUNT(*) c, COUNT(DISTINCT user_id) u "
                "FROM funnel_events WHERE ts >= ? AND event = 'priv_view' "
                "GROUP BY branch ORDER BY c DESC",
                (since,),
            ).fetchall()
            if branches:
                lines.append("priv_view по branch:")
                for r in branches:
                    label = r["branch"] or "(пусто)"
                    lines.append(
                        f"  · <code>{label}</code>: {int(r['c'])} "
                        f"({int(r['u'])} чел.)"
                    )
            win = {
                int(r["user_id"])
                for r in conn.execute(
                    "SELECT DISTINCT user_id FROM funnel_events "
                    "WHERE ts >= ? AND event = 'win_shown'",
                    (since,),
                ).fetchall()
            }
            menu = {
                int(r["user_id"])
                for r in conn.execute(
                    "SELECT DISTINCT user_id FROM funnel_events "
                    "WHERE ts >= ? AND event = 'priv_menu'",
                    (since,),
                ).fetchall()
            }
            if win:
                pct = 100.0 * len(win & menu) / len(win)
                lines.append(
                    f"win_shown → priv_menu: {len(win & menu)}/{len(win)} "
                    f"({pct:.0f}%)"
                )
            else:
                lines.append("win_shown → priv_menu: нет win_shown")
    return "\n".join(lines)


_FUNNEL_EXPORT_COLS = (
    "win_shown",
    "priv_menu",
    "priv_cmd",
    "view_offer",
    "view_file",
    "view_basic",
    "view_group",
    "view_vip",
)
_FUNNEL_VIEW_BRANCH = {
    "offer": "view_offer",
    "file": "view_file",
    "basic": "view_basic",
    "group": "view_group",
    "vip": "view_vip",
}


def funnel_export_tsv(days: int = 30) -> str:
    """Daily raw counts for Google Sheets. One TSV line per calendar UTC day.

    Days with no events are still emitted as zeros so the sheet has no holes.
    ``days`` is inclusive of today and must be 1..90.
    """
    n = int(days)
    if n > 90:
        raise ValueError("days > 90")
    if n < 1:
        raise ValueError("days < 1")
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=n - 1)
    start_ts = int(
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
    )
    end = today + timedelta(days=1)
    end_ts = int(
        datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()
    )
    buckets: dict[str, dict[str, int]] = {}
    day = start
    while day <= today:
        buckets[day.isoformat()] = {col: 0 for col in _FUNNEL_EXPORT_COLS}
        day += timedelta(days=1)
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, event, branch FROM funnel_events "
            "WHERE ts >= ? AND ts < ?",
            (start_ts, end_ts),
        ).fetchall()
    for r in rows:
        key = datetime.fromtimestamp(int(r["ts"]), tz=timezone.utc).date().isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        ev = str(r["event"] or "")
        if ev in ("win_shown", "priv_menu", "priv_cmd"):
            bucket[ev] += 1
        elif ev == "priv_view":
            col = _FUNNEL_VIEW_BRANCH.get(str(r["branch"] or ""))
            if col:
                bucket[col] += 1
    lines = []
    for d in sorted(buckets):
        b = buckets[d]
        lines.append(
            "\t".join(
                [d] + [str(b[col]) for col in _FUNNEL_EXPORT_COLS]
            )
        )
    return "\n".join(lines)
