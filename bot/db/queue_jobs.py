"""Persistent queue store: submit_jobs leases + submit_job_rows.

The in-memory queue in bot/submit_queue.py stays the runtime view, but this
module makes SQLite able to *be* the source of truth when the workers move
onto it. Nothing here imports bot.limits or bot.registry — claim policy
(per-user parallel, per-project lanes, runtime-disable) is injected as
callables, so the dependency arrow stays db → nowhere.

Lease protocol (one claim = one transaction):

    BEGIN IMMEDIATE
    sweep leases expired before now        (back to queued, or error)
    pick lowest-id queued job passing the lane guards
    UPDATE status='running', lease_owner=me, lease_until=now+T, attempts+=1
    COMMIT

    ... work, renewing the lease every T/3 ...

    renew_lease / update_progress / finish_job   (all owner-guarded)

An owner-guarded write is an ``UPDATE ... WHERE id=? AND lease_owner=? AND
status='running'`` — a worker whose lease expired and was reclaimed must not
be able to speak for the job anymore, least of all to finish it.

**Fencing.** Owner alone is not enough for row writes: worker A can lose its
lease mid-HTTP, the job is reclaimed, and A's late answer would still land
on the row unless A's *generation* is checked too. Every mutating call
therefore takes ``(owner, generation)`` and matches BOTH:

    ... WHERE lease_owner=? AND attempts=? AND status='running'

``attempts`` is bumped by every claim, so it doubles as the fencing token —
even the same worker name re-claiming the job later gets a new generation
and the old one goes stale. "A stale worker is mute" is an invariant of the
whole API, not of three of its functions.

Cancel and expiry compose: an expired lease on a job whose user (or admin)
requested a cancel is finished as ``cancelled`` with its rows dropped —
never requeued into a claim scan that would skip it forever.

The one hazard a lease cannot remove: a row in flight when the lease expires
may be submitted twice (old owner sent it, new owner sends it again). That
is deliberately the same risk the restart path already accepts and the
global wallet dedup already answers — the project answers ALREADY and the
duplicate burns no slot. A shorter lease narrows the window; it cannot
close it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from bot.db import _now, db

# Columns added to submit_jobs for the lease protocol. Keep in sync with
# ensure_schema below; every add is PRAGMA-guarded so any database — fresh,
# old, or restored from a backup past its stamp — converges.
_JOB_LEASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
    ("lease_until", "TEXT"),
    ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("batch_id", "INTEGER NOT NULL DEFAULT 0"),
    ("batch_total", "INTEGER NOT NULL DEFAULT 0"),
    ("parallel", "INTEGER NOT NULL DEFAULT 1"),
    ("auto_reg", "INTEGER NOT NULL DEFAULT 0"),
    ("skipped_dup", "INTEGER NOT NULL DEFAULT 0"),
)

DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_MAX_ATTEMPTS = 3

TERMINAL_STATUSES = ("done", "cancelled", "error")


def _iso_in(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))
    ).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: lease columns on submit_jobs + the submit_job_rows table.

    Called from submit_queue.init_schema (which creates submit_jobs on fresh
    databases) and from the numbered schema migration, so a database reaches
    this shape no matter which code ran first.
    """
    existing = {
        str(r["name"])
        for r in conn.execute("PRAGMA table_info(submit_jobs)").fetchall()
    }
    if existing:
        # Table present (fresh DBs get the columns inline instead). The claim
        # index needs the table to exist — a fresh database runs migrations
        # before submit_queue.init_schema creates it.
        for col, decl in _JOB_LEASE_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE submit_jobs ADD COLUMN {col} {decl}")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_submit_jobs_claim
                ON submit_jobs(status, id)
            """
        )
        # Cross-process duplicate backstop: whatever the application-level
        # gate misses (two writers, one per process), SQLite itself refuses
        # to hold two open jobs for the same user on the same project. A
        # pre-existing database that violates it (never produced by the
        # in-memory queue, but a restored backup cannot be trusted) simply
        # keeps running without the index — the gate still checks counts.
        try:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_submit_jobs_user_project_open
                    ON submit_jobs(user_id, project_id)
                    WHERE status IN ('queued', 'running')
                """
            )
        except sqlite3.IntegrityError:
            # Never fail schema init over the index; leave a readable trace.
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) "
                    "VALUES ('queue_duplicate_index_skipped', '1', ?)",
                    (_now(),),
                )
            except sqlite3.Error:
                pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submit_job_rows (
            job_id      INTEGER NOT NULL,
            seq         INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT NOT NULL DEFAULT '',
            updated_at  TEXT,
            PRIMARY KEY (job_id, seq)
        )
        """
    )
    # Durable «one batch per user» gate (replaces the in-process
    # _user_batch_pending set — two processes must not start two batches).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_batch_gates (
            user_id     INTEGER PRIMARY KEY,
            batch_id    INTEGER NOT NULL,
            created_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _insert_job_locked(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    project_id: str,
    chat_id: int,
    message_id: int | None,
    rows: list[dict],
    skipped_dup: int = 0,
    batch_id: int = 0,
    batch_total: int = 0,
    auto_reg: bool = False,
    parallel: int = 1,
    now: str,
) -> int:
    """Caller holds an open transaction. Insert job + every row."""
    import json

    cur = conn.execute(
        """
        INSERT INTO submit_jobs
          (user_id, project_id, status, total, done, ok, fail, already,
           bad, chat_id, message_id, detail, created_at,
           skipped_dup, batch_id, batch_total, auto_reg, parallel)
        VALUES (?, ?, 'queued', ?, 0, 0, 0, 0, 0, ?, ?, '', ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            str(project_id),
            len(rows),
            int(chat_id),
            message_id,
            now,
            int(skipped_dup or 0),
            int(batch_id or 0),
            int(batch_total or 0),
            1 if auto_reg else 0,
            max(1, int(parallel or 1)),
        ),
    )
    job_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO submit_job_rows (job_id, seq, payload_json, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (job_id, i, json.dumps(r, ensure_ascii=False), now)
            for i, r in enumerate(rows)
        ],
    )
    return job_id


def create_job(
    *,
    user_id: int,
    project_id: str,
    chat_id: int,
    message_id: int | None,
    rows: list[dict],
    skipped_dup: int = 0,
    batch_id: int = 0,
    batch_total: int = 0,
    auto_reg: bool = False,
    parallel: int = 1,
) -> int:
    """Insert the job and every row in ONE transaction — no payload files.

    The old two-step (DB row, then {id}.json) could not be made atomic across
    a crash and is why BOOT_RESUME_JOBS exists. With rows in the same
    transaction as the job, there is no state where one exists without the
    other.
    """
    now = _now()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job_id = _insert_job_locked(
                conn,
                user_id=user_id,
                project_id=project_id,
                chat_id=chat_id,
                message_id=message_id,
                rows=rows,
                skipped_dup=skipped_dup,
                batch_id=batch_id,
                batch_total=batch_total,
                auto_reg=auto_reg,
                parallel=parallel,
                now=now,
            )
            conn.commit()
            return job_id
        except Exception:
            conn.rollback()
            raise


def create_job_if_allowed(
    *,
    user_id: int,
    project_id: str,
    chat_id: int,
    message_id: int | None,
    rows: list[dict],
    skipped_dup: int = 0,
    batch_id: int = 0,
    batch_total: int = 0,
    auto_reg: bool = False,
    parallel: int = 1,
    max_user_jobs_fn: Callable[[int], int],
    project_enabled_fn: Callable[[str], bool],
) -> tuple[int | None, str]:
    """SQLite-atomic enqueue gate — the durable twin of enqueue_if_allowed.

    The Python-``_lock`` version checked runtime-disable / same-project
    duplicate / per-user cap and then inserted, which is only atomic while
    every writer lives in one process. This runs the checks and the insert
    inside one ``BEGIN IMMEDIATE``: two processes (bot + a future standalone
    webapp) cannot both pass the gate, and the partial unique index is the
    backstop should a check be forgotten.

    Returns ``(job_id, "")`` or ``(None, reason)`` with reason one of
    ``"disabled"``, ``"duplicate"``, ``"full"`` — the strings the live
    callers already switch on.
    """
    uid = int(user_id)
    pid = str(project_id)
    now = _now()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not project_enabled_fn(pid):
                conn.commit()
                return None, "disabled"
            open_row = conn.execute(
                "SELECT 1 FROM submit_jobs WHERE user_id=? AND project_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (uid, pid),
            ).fetchone()
            if open_row is not None:
                conn.commit()
                return None, "duplicate"
            n_active = conn.execute(
                "SELECT COUNT(*) n FROM submit_jobs WHERE user_id=? "
                "AND status IN ('queued','running')",
                (uid,),
            ).fetchone()
            if int(n_active["n"]) >= max(1, max_user_jobs_fn(uid)):
                conn.commit()
                return None, "full"
            try:
                job_id = _insert_job_locked(
                    conn,
                    user_id=uid,
                    project_id=pid,
                    chat_id=chat_id,
                    message_id=message_id,
                    rows=rows,
                    skipped_dup=skipped_dup,
                    batch_id=batch_id,
                    batch_total=batch_total,
                    auto_reg=auto_reg,
                    parallel=parallel,
                    now=now,
                )
            except sqlite3.IntegrityError:
                # The partial unique index spoke: the checks raced a writer
                # that committed between our SELECT and INSERT — impossible
                # under BEGIN IMMEDIATE, but a belt for a future code path.
                conn.rollback()
                return None, "duplicate"
            conn.commit()
            return job_id, ""
        except Exception:
            conn.rollback()
            raise


def _sweep_expired_locked(
    conn: sqlite3.Connection, *, now: str, max_attempts: int
) -> int:
    """Caller holds an open BEGIN IMMEDIATE. Settle expired leases.

    Three outcomes, most specific first:

    * cancel was requested → terminal ``cancelled``, rows dropped. Requeueing
      would strand it forever: the claim scan skips ``cancel_requested`` jobs,
      so ``queued + cancel_requested=1`` is a job that looks open and is
      claimed by nobody (caught in review before the workers moved on).
    * attempts exhausted → terminal ``error``/``lease_expired``, rows dropped
      (a dead job's payloads are retention, not state).
    * otherwise → back to ``queued``, lease cleared. Rows already marked done
      keep their status — a reclaim resumes after them, not over them.
    """
    expired = conn.execute(
        "SELECT id, attempts, cancel_requested FROM submit_jobs "
        "WHERE status='running' AND lease_until IS NOT NULL AND lease_until < ?",
        (now,),
    ).fetchall()
    for row in expired:
        jid = int(row["id"])
        if int(row["cancel_requested"]):
            conn.execute(
                "UPDATE submit_jobs SET status='cancelled', "
                "detail=COALESCE(NULLIF(detail,''),'cancelled'), finished_at=?, "
                "lease_owner='', lease_until=NULL WHERE id=?",
                (now, jid),
            )
            conn.execute("DELETE FROM submit_job_rows WHERE job_id=?", (jid,))
        elif int(row["attempts"]) >= max_attempts:
            conn.execute(
                "UPDATE submit_jobs SET status='error', detail='lease_expired', "
                "finished_at=?, lease_owner='', lease_until=NULL WHERE id=?",
                (now, jid),
            )
            conn.execute("DELETE FROM submit_job_rows WHERE job_id=?", (jid,))
        else:
            conn.execute(
                "UPDATE submit_jobs SET status='queued', lease_owner='', "
                "lease_until=NULL WHERE id=?",
                (jid,),
            )
            # Rows in flight under the dead lease become pending again, in
            # the same transaction as the requeue. Without this the protocol
            # contradicts itself: pending_rows() counts 'running' as
            # retryable, but mark_row_running() only takes 'pending' — a
            # reclaimed job would list the row and be unable to start it.
            # A double dispatch within ONE generation stays blocked; only a
            # new generation (a new claim after a dead lease) may re-take.
            conn.execute(
                "UPDATE submit_job_rows SET status='pending', updated_at=? "
                "WHERE job_id=? AND status='running'",
                (now, jid),
            )
    return len(expired)


def claim_next_job(
    owner: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    *,
    user_parallel_fn: Callable[[int], int] | None = None,
    project_limit_fn: Callable[[str], int] | None = None,
    project_claimable_fn: Callable[[str], bool] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict | None:
    """Claim the FIFO-next claimable job, or None.

    Lane rules are the ones the in-memory worker enforces, asked as
    callables to keep this module free of limits/registry imports:
    per-user running cap (``user_parallel_fn(uid)``), per-project cap
    (``project_limit_fn(project_id)``), and whether the project may take
    work at all (``project_claimable_fn`` — the runtime-disable gate;
    returning False leaves the job queued, the same cancel path that
    disables a project also cancels its jobs). A job whose cancel was
    requested while queued is skipped — the cancel path finishes it
    directly.

    ``attempts`` in the returned dict is the fencing generation: pass it
    back with ``owner`` into every mutating call.
    """
    if not owner:
        raise ValueError("claim_next_job: owner must be a non-empty name")
    now = _now()
    lease_until = _iso_in(lease_seconds)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _sweep_expired_locked(conn, now=now, max_attempts=max_attempts)
            candidates = conn.execute(
                "SELECT * FROM submit_jobs WHERE status='queued' "
                "AND cancel_requested=0 ORDER BY id"
            ).fetchall()
            running_user: dict[int, int] = {}
            running_proj: dict[str, int] = {}
            for r in conn.execute(
                "SELECT user_id, project_id, COUNT(*) n FROM submit_jobs "
                "WHERE status='running' GROUP BY user_id, project_id"
            ):
                running_user[int(r["user_id"])] = (
                    running_user.get(int(r["user_id"]), 0) + int(r["n"])
                )
                running_proj[str(r["project_id"])] = (
                    running_proj.get(str(r["project_id"]), 0) + int(r["n"])
                )
            for row in candidates:
                uid = int(row["user_id"])
                pid = str(row["project_id"])
                if project_claimable_fn is not None and not project_claimable_fn(pid):
                    continue
                if user_parallel_fn is not None and running_user.get(uid, 0) >= max(
                    1, user_parallel_fn(uid)
                ):
                    continue
                if project_limit_fn is not None and running_proj.get(pid, 0) >= max(
                    1, project_limit_fn(pid)
                ):
                    continue
                conn.execute(
                    "UPDATE submit_jobs SET status='running', lease_owner=?, "
                    "lease_until=?, attempts=attempts+1, "
                    "started_at=COALESCE(started_at, ?) WHERE id=?",
                    (owner, lease_until, now, int(row["id"])),
                )
                conn.commit()
                fresh = conn.execute(
                    "SELECT * FROM submit_jobs WHERE id=?", (int(row["id"]),)
                ).fetchone()
                return dict(fresh) if fresh else None
            conn.commit()
            return None
        except Exception:
            conn.rollback()
            raise


def renew_lease(
    job_id: int,
    owner: str,
    generation: int,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend our lease. False = the job is no longer ours to speak for."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE submit_jobs SET lease_until=? WHERE id=? AND lease_owner=? "
            "AND attempts=? AND status='running'",
            (_iso_in(lease_seconds), int(job_id), owner, int(generation)),
        )
        conn.commit()
        return cur.rowcount > 0


def update_progress(
    job_id: int,
    owner: str,
    generation: int,
    *,
    total: int | None = None,
    done: int | None = None,
    ok: int | None = None,
    fail: int | None = None,
    already: int | None = None,
    bad: int | None = None,
    message_id: int | None = None,
) -> bool:
    """Fenced progress write; a stale worker cannot move the counters."""
    sets, params = [], []
    for name, val in (
        ("total", total), ("done", done), ("ok", ok), ("fail", fail),
        ("already", already), ("bad", bad), ("message_id", message_id),
    ):
        if val is not None:
            sets.append(f"{name}=?")
            params.append(val)
    if not sets:
        return False
    params += [int(job_id), owner, int(generation)]
    with db() as conn:
        cur = conn.execute(
            f"UPDATE submit_jobs SET {', '.join(sets)} "
            "WHERE id=? AND lease_owner=? AND attempts=? AND status='running'",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def finish_job(
    job_id: int,
    owner: str,
    generation: int,
    status: str,
    *,
    detail: str = "",
    counters: dict[str, int] | None = None,
    drop_rows: bool = True,
) -> bool:
    """Terminal, fenced. Clears the lease and by default drops rows.

    Rows are the payload's replacement: once terminal they have served their
    purpose (history lives in submissions + the job's counters), and keeping
    a thousand payload_json blobs per VIP job is the old payload-dir problem
    back in a table.
    """
    if status not in ("done", "cancelled", "error"):
        raise ValueError(f"finish_job: terminal status expected, got {status!r}")
    sets = ["status=?", "detail=?", "finished_at=?", "lease_owner=''",
            "lease_until=NULL"]
    params: list[Any] = [status, str(detail)[:500], _now()]
    for key in ("total", "done", "ok", "fail", "already", "bad"):
        val = (counters or {}).get(key)
        if val is not None:
            sets.append(f"{key}=?")
            params.append(int(val))
    params += [int(job_id), owner, int(generation)]
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"UPDATE submit_jobs SET {', '.join(sets)} "
            "WHERE id=? AND lease_owner=? AND attempts=? "
            "AND status IN ('running','queued')",
            params,
        )
        ok = cur.rowcount > 0
        if ok and drop_rows:
            conn.execute("DELETE FROM submit_job_rows WHERE job_id=?", (int(job_id),))
        conn.commit()
        return ok


def request_cancel(job_id: int, *, detail: str = "cancel requested") -> bool:
    """Cooperative cancel. A queued job dies here; a running one is flagged
    and stops between rows (the owner reads cancel_requested on progress)."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM submit_jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        if row is None or str(row["status"]) not in ("queued", "running"):
            conn.commit()
            return False
        if str(row["status"]) == "queued":
            conn.execute(
                "UPDATE submit_jobs SET status='cancelled', cancel_requested=1, "
                "detail=?, finished_at=?, lease_owner='', lease_until=NULL WHERE id=?",
                (str(detail)[:500], _now(), int(job_id)),
            )
            conn.execute(
                "DELETE FROM submit_job_rows WHERE job_id=?", (int(job_id),)
            )
        else:
            conn.execute(
                "UPDATE submit_jobs SET cancel_requested=1, detail=? WHERE id=?",
                (str(detail)[:500], int(job_id)),
            )
        conn.commit()
        return True


def _fenced(
    conn: sqlite3.Connection, job_id: int, owner: str, generation: int
) -> bool:
    """True when (owner, generation) still holds a running lease on the job."""
    row = conn.execute(
        "SELECT 1 FROM submit_jobs WHERE id=? AND lease_owner=? AND attempts=? "
        "AND status='running'",
        (int(job_id), owner, int(generation)),
    ).fetchone()
    return row is not None


def mark_row_running(job_id: int, seq: int, owner: str, generation: int) -> bool:
    """Fence the row before the HTTP call it belongs to.

    Sets pending → running so a reclaim sees the row as retryable work in
    flight (``pending_rows`` includes running), and so a stale worker that
    loses the race here knows not to submit at all: a False return means the
    lease was lost — do not send the request.
    """
    with db() as conn:
        cur = conn.execute(
            "UPDATE submit_job_rows SET status='running', attempts=attempts+1, "
            "updated_at=? WHERE job_id=? AND seq=? AND status='pending' "
            "AND EXISTS (SELECT 1 FROM submit_jobs WHERE id=? AND lease_owner=? "
            "AND attempts=? AND status='running')",
            (_now(), int(job_id), int(seq), int(job_id), owner, int(generation)),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_row_done(
    job_id: int,
    seq: int,
    owner: str,
    generation: int,
    *,
    status: str = "done",
    error: str = "",
) -> bool:
    """Fenced row outcome. A worker that lost the lease cannot record it.

    The row is idempotent to rewrite for the *current* holder; the fence is
    what stops a reclaimed worker's late HTTP answer from deciding anything.
    """
    with db() as conn:
        cur = conn.execute(
            "UPDATE submit_job_rows SET status=?, attempts=attempts+1, "
            "last_error=?, updated_at=? WHERE job_id=? AND seq=? "
            "AND status IN ('pending','running') "
            "AND EXISTS (SELECT 1 FROM submit_jobs WHERE id=? AND lease_owner=? "
            "AND attempts=? AND status='running')",
            (
                status, str(error)[:500], _now(),
                int(job_id), int(seq),
                int(job_id), owner, int(generation),
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def load_job(job_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM submit_jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        return dict(row) if row else None


def job_rows(job_id: int) -> list[tuple[int, dict]]:
    """(seq, payload) in seq order; pending rows first for a resume."""
    import json

    with db() as conn:
        rows = conn.execute(
            "SELECT seq, payload_json, status FROM submit_job_rows "
            "WHERE job_id=? ORDER BY seq",
            (int(job_id),),
        ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except ValueError:
            continue
        out.append((int(r["seq"]), payload))
    return out


def pending_rows(job_id: int) -> list[tuple[int, dict]]:
    """Rows not yet finished — what a reclaimed job still has to run."""
    import json

    with db() as conn:
        rows = conn.execute(
            "SELECT seq, payload_json FROM submit_job_rows "
            "WHERE job_id=? AND status IN ('pending','running') ORDER BY seq",
            (int(job_id),),
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append((int(r["seq"]), json.loads(r["payload_json"])))
        except ValueError:
            continue
    return out


def open_jobs(user_id: int | None = None) -> list[dict]:
    """Queued+running jobs (one user or everybody), oldest first."""
    if user_id is None:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM submit_jobs WHERE status IN ('queued','running') "
                "ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM submit_jobs WHERE user_id=? AND status IN "
            "('queued','running') ORDER BY id",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Durable «one batch per user» gate ---
# The in-process `_user_batch_pending` set closes a check-then-enqueue race
# between two parallel taps; with a second writer process it proves nothing.
# The gate row plays the same role in SQLite: present = a batch is starting.

BATCH_GATE_TTL_SECONDS = 3600.0


def begin_user_batch(
    user_id: int, batch_id: int, *, ttl_seconds: float = BATCH_GATE_TTL_SECONDS
) -> bool:
    """Atomically claim the user's batch slot. True = caller may enqueue.

    A gate older than the TTL is stolen rather than obeyed: a crash between
    begin and end must not lock the user out of batches forever, and the
    enqueue loop for a live batch finishes in seconds — an hour-old gate is
    a corpse, not a batch.
    """
    uid = int(user_id)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=max(60.0, ttl_seconds))).isoformat()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stale = conn.execute(
                "SELECT batch_id, created_at FROM user_batch_gates WHERE user_id=?",
                (uid,),
            ).fetchone()
            if stale is not None and str(stale["created_at"]) >= cutoff:
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO user_batch_gates (user_id, batch_id, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "batch_id=excluded.batch_id, created_at=excluded.created_at",
                (uid, int(batch_id), now.isoformat()),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def end_user_batch(user_id: int) -> None:
    """Release the gate once the enqueue loop is done.

    From that point the batch is tracked the way it always was: by its open
    jobs (`batch_id` on queued/running rows), which survive any process.
    """
    with db() as conn:
        conn.execute(
            "DELETE FROM user_batch_gates WHERE user_id=?", (int(user_id),)
        )
        conn.commit()


def user_batch_gate(user_id: int) -> int:
    """Current gate row's batch_id, or 0 (no gate)."""
    with db() as conn:
        row = conn.execute(
            "SELECT batch_id FROM user_batch_gates WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    return int(row["batch_id"]) if row else 0
