"""Global FIFO submit queue with progress messages.

One job = all rows from user's /data file for one project.
Users may chain up to USER_MAX_ACTIVE_JOBS jobs (default 3). Jobs in different
projects use independent lanes; the same project is processed by one lane at
a time, while different projects may run concurrently.

Global workers = BOT_CONCURRENCY. How many rows a job submits in parallel comes
from the user's tier (bot.limits.job_parallel): free/legacy tiers = 1 row at
a time, paid and VIP tiers up to VIP_JOB_PARALLEL.

Source of truth is SQLite (bot/db/queue_jobs.py): enqueue, claim, progress
and finish are lease-protocol writes fenced by (owner, generation). The
in-memory structures below are a render cache (finished jobs are dropped from
it after a while; everything is reloadable from the DB). A crash costs
exactly one lease timeout: the sweep requeues the job with its running rows
back to pending, and the next worker resumes — no BOOT_RESUME_JOBS replay,
no zombie reconciliation.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bot import config
from bot import db as bot_db
from bot.db import queue_jobs as qj
from bot import limits as bot_limits
from bot import screen
from bot import tweet_urls
from bot import user_data as user_data_mod
from bot.http_client import is_proxy_fail_detail, log
from bot.i18n import t
from bot.registry import Project, get_project
from bot.ux import esc, progress_bar, remaining, safe_send

# Own /proxy in use: after this many consecutive proxy-transport FAILs in one
# job, tell the user once. Shared-pool failures stay silent — those are ours.
OWN_PROXY_FAIL_ALERT_STREAK = 20


def project_display_name(project_id: str) -> str:
    """Resolve project display name; fall back to id."""
    p = get_project(project_id, allow_runtime_disabled=True)
    name = ""
    try:
        name = (p.name if p else "") or ""
    except Exception:
        name = ""
    name = name.strip()
    return name or project_id

SubmitFn = Callable[..., dict]
# submit_fn(project, fields, user_id) -> result dict with status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_dir() -> Path:
    d = config.JOB_PAYLOAD_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _enabled_project_count() -> int:
    try:
        from bot.registry import list_projects

        n = len(list_projects())
        return n if n > 0 else 1
    except Exception:
        return 1


def _max_user_jobs(user_id: int | None = None, *, auto_reg: bool = False) -> int:
    """Queue depth for a user: base cap, raised for VIP so a batch fits.

    Auto-reg uses the same raised depth so every new project can sit in the
    queue (operator, 2026-09-25). Only the *queue* grows: how many of those
    jobs run at once is still ``bot_limits.parallel_jobs``.
    """
    base = max(1, int(getattr(config, "USER_MAX_ACTIVE_JOBS", 3) or 3))
    if auto_reg:
        return max(base, _enabled_project_count())
    if user_id is None:
        return base
    try:
        if not bot_limits.is_vip(int(user_id)):
            return base
    except Exception:
        return base
    configured = int(getattr(config, "VIP_MAX_ACTIVE_JOBS", 0) or 0)
    if configured <= 0:
        configured = _enabled_project_count()
    return max(base, configured)


def resolve_job_parallel(project, user_id: int) -> int:
    """Rows this job may submit at once: tier, then the project's own limits.

    Order matters. The tier (`bot.limits.job_parallel`) is what the user paid
    or was granted; `queue_parallel` lets a project pin an exact number;
    `queue_parallel_vip` overrides that pin for admin-granted VIPs; and
    `max_job_parallel` is a ceiling the project cannot exceed whatever the tier
    says. The ceiling exists because an Apps Script sheet serializes writes: 20
    threads do not make it 20x faster, they make it answer "Lock timeout" — and
    a lost write can still redirect exactly like a successful one, so the row
    is recorded OK while the sheet stays empty.

    The VIP override exists for the other kind of throttle: one adopted as a
    precaution rather than measured off a failing endpoint. There, single-file
    is the safe default for everybody and the handful of trusted accounts keep
    their threads — a distinction one pinned number cannot express.
    """
    n_par = 1
    try:
        n_par = bot_limits.job_parallel(user_id)
    except Exception:
        n_par = 1
    queue_parallel = getattr(project, "queue_parallel", None)
    if queue_parallel is not None:
        n_par = max(1, int(queue_parallel))
    vip_parallel = getattr(project, "queue_parallel_vip", None)
    if vip_parallel is not None:
        try:
            if bot_limits.is_vip(int(user_id)):
                n_par = max(1, int(vip_parallel))
        except Exception:
            pass
    max_par = getattr(project, "max_job_parallel", None)
    if max_par is not None:
        n_par = min(n_par, max(1, int(max_par)))
    return max(1, int(n_par))


def _project_parallel_limit(project_id: str = "") -> int:
    """Jobs allowed to run at once for a project.

    A project may pin its own value (`project_concurrency` in project.toml):
    one whose rows are heavy — many threads, minutes per row — is better served
    one job at a time than sharing the global HTTP budget with itself.
    """
    default = max(1, int(getattr(config, "PROJECT_CONCURRENCY", 3) or 3))
    if not project_id:
        return default
    project = get_project(project_id, allow_runtime_disabled=True)
    own = getattr(project, "project_concurrency", None) if project else None
    return max(1, int(own)) if own else default


@dataclass
class QueueJob:
    id: int
    user_id: int
    project_id: str
    chat_id: int
    message_id: int | None
    status: str  # queued | running | done | cancelled | error
    total: int = 0
    done: int = 0
    ok: int = 0
    fail: int = 0
    already: int = 0
    bad: int = 0
    detail: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    # Lease identity while this job runs (empty for queued/finished views).
    # `generation` is the job's `attempts` at claim time — the fencing token
    # every DB write must present alongside owner.
    owner: str = ""
    generation: int = 0
    # in-memory rows for running (loaded from submit_job_rows)
    rows: list[dict] = field(default_factory=list)
    # set by cancel paths; worker stops between rows (DB: cancel_requested)
    cancel_requested: bool = False
    # stop remaining rows when OK limit hit mid-job
    limit_reached: bool = False
    # VIP only: how many parallel row submits (set when job starts)
    parallel: int = 1
    # wallets skipped at prepare (already OK/ALREADY in bot DB)
    skipped_dup: int = 0
    # VIP «all projects» batch this job belongs to (0 = single-project start).
    # In-memory only: after a restart the batch link is gone, which costs
    # nothing — the per-project duplicate gate still prevents a second batch
    # from double-queueing the same project.
    batch_id: int = 0
    # how many projects the batch started with (for «project 7/20» progress)
    batch_total: int = 0
    # Own /proxy alert: set at job start from has_own_proxies; streak counts
    # consecutive proxy-transport FAILs and fires one DM at the threshold.
    uses_own_proxies: bool = False
    own_proxy_fail_streak: int = 0
    own_proxy_alert_sent: bool = False
    # Auto-registration run (bot/auto_reg.py): the user gets one personal DM
    # with the result when the job ends. In-memory only, like batch_id.
    auto_reg: bool = False
    # Display name of the project, for the finish DM.
    project_name: str = ""
    # (seq, payload) pairs while running (the DB is the authority).
    _seq_rows: list[tuple[int, dict]] | None = None


_lock = threading.RLock()
# Render cache only — the DB is the authority. `_jobs` holds running jobs the
# workers are mutating (counters, proxy streaks) and recently finished ones
# until _purge_job_soon drops them; every view below re-reads SQLite.
_jobs: dict[int, QueueJob] = {}
_next_batch_id = 0
_workers_started = False
_bot = None
_submit_fn: SubmitFn | None = None
_stop = threading.Event()

# Lease sizing: a worker renews every LEASE_SECONDS/3; a dead process costs
# one lease timeout before its job is reclaimed and resumed.
LEASE_SECONDS = float(os.getenv("QUEUE_LEASE_SECONDS", "120") or 120)


def _worker_owner(worker_id: int) -> str:
    return f"{os.uname().nodename}-{os.getpid()}-w{worker_id}"


def _cleanup_project_job(job: QueueJob) -> None:
    """Best-effort cleanup for project-owned in-memory queue state."""
    project = get_project(job.project_id, allow_runtime_disabled=True)
    hook = getattr(project, "queue_cleanup", None) if project is not None else None
    if hook is None:
        return
    try:
        hook(rows=list(job.rows), user_id=job.user_id, job_id=job.id)
    except Exception as exc:
        log(f"[queue] cleanup job={job.id} project={job.project_id}: {exc}")


def _deliver_project_job(job: QueueJob) -> None:
    """Let the project hand this job's artifact to its owner.

    Called once when a job stops running — including a user cancel, because a
    half-finished batch still produced wallets the user paid slots for. Runs
    before cleanup, which is free to drop the in-memory state it reads.
    """
    project = get_project(job.project_id, allow_runtime_disabled=True)
    hook = getattr(project, "queue_deliver", None) if project is not None else None
    if hook is None:
        return
    try:
        hook(
            rows=list(job.rows),
            user_id=job.user_id,
            job_id=job.id,
            status=job.status,
            bot=_bot,
        )
    except Exception as exc:
        log(f"[queue] deliver job={job.id} project={job.project_id}: {exc}")


def init_schema() -> None:
    with bot_db.db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submit_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                fail INTEGER NOT NULL DEFAULT 0,
                already INTEGER NOT NULL DEFAULT 0,
                bad INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER,
                message_id INTEGER,
                detail TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
            """
        )
        # The only table here that had no index at all, and the mini app reads
        # it twice per poll — once for this user's active jobs, once for their
        # recent ones — so both were full scans of every job the bot has ever
        # run. Cheap today at a few thousand rows; the table only grows, and
        # the poll is every four seconds per open screen.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_submit_jobs_user_status
                ON submit_jobs(user_id, status, id)
            """
        )
        # Lease columns + submit_job_rows (persistent-queue groundwork).
        # PRAGMA-guarded, so a legacy DB converges here even if it never ran
        # schema migration 7. Bound via the module-level import: a late
        # `from bot.db.queue_jobs import ...` here would resolve through
        # sys.modules and, in the test suite's purge-and-reimport world,
        # land on a different module instance with a different DB.
        qj.ensure_schema(conn)
        conn.commit()


def _row_to_job(row: dict) -> QueueJob:
    """DB row → QueueJob view. `rows` stays empty until the worker loads it."""
    return QueueJob(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        project_id=str(row["project_id"]),
        chat_id=int(row["chat_id"] or 0),
        message_id=int(row["message_id"]) if row["message_id"] else None,
        status=str(row["status"]),
        total=int(row["total"] or 0),
        done=int(row["done"] or 0),
        ok=int(row["ok"] or 0),
        fail=int(row["fail"] or 0),
        already=int(row["already"] or 0),
        bad=int(row["bad"] or 0),
        detail=str(row["detail"] or ""),
        created_at=str(row.get("created_at") or ""),
        started_at=str(row.get("started_at") or ""),
        finished_at=str(row.get("finished_at") or ""),
        cancel_requested=bool(row.get("cancel_requested")),
        parallel=max(1, int(row.get("parallel") or 1)),
        skipped_dup=int(row.get("skipped_dup") or 0),
        batch_id=int(row.get("batch_id") or 0),
        batch_total=int(row.get("batch_total") or 0),
        auto_reg=bool(row.get("auto_reg")),
    )


def _cached_or_row(row: dict) -> QueueJob:
    """Prefer the in-memory running job (live counters) over the DB snapshot."""
    jid = int(row["id"])
    with _lock:
        live = _jobs.get(jid)
    if live is not None and live.status in ("queued", "running"):
        return live
    return _row_to_job(row)


def user_active_jobs(user_id: int) -> list[QueueJob]:
    """Queued+running jobs for user, oldest first (SQLite authority)."""
    return [_cached_or_row(r) for r in qj.open_jobs(int(user_id))]


def user_has_active_job(user_id: int) -> QueueJob | None:
    """Prefer running job, else first queued (for cancel / short status)."""
    jobs = user_active_jobs(user_id)
    if not jobs:
        return None
    for j in jobs:
        if j.status == "running":
            return j
    return jobs[0]


def user_running_job(user_id: int) -> QueueJob | None:
    for j in user_active_jobs(user_id):
        if j.status == "running":
            return j
    return None


def user_active_job_count(user_id: int) -> int:
    return len(user_active_jobs(user_id))


def user_has_project_job(user_id: int, project_id: str) -> QueueJob | None:
    pid = (project_id or "").strip()
    for j in user_active_jobs(user_id):
        if j.project_id == pid:
            return j
    return None


def can_enqueue_user(user_id: int) -> bool:
    return user_active_job_count(user_id) < _max_user_jobs(user_id)


def max_user_jobs(user_id: int) -> int:
    """Effective per-user cap (VIP is raised — what refusal texts should say)."""
    return _max_user_jobs(int(user_id))


def user_batch_jobs(user_id: int) -> list[QueueJob]:
    """Active jobs belonging to this user's «all projects» batch."""
    return [j for j in user_active_jobs(user_id) if j.batch_id]


def user_batch_active(user_id: int) -> bool:
    """True while a batch of this user still has queued/running jobs."""
    return bool(user_batch_jobs(user_id))


# «queue:all» starts are gated durably (bot/db/queue_jobs.py: user_batch_gates)
# — the check-then-enqueue race has to hold across processes now.


def try_begin_user_batch(user_id: int) -> bool:
    """Atomically claim the user's «one batch at a time» slot (10.1).

    True means the caller may enqueue batch jobs. Release with
    ``end_user_batch()`` once the enqueue loop is done — the enqueued jobs
    then carry the gate via ``user_batch_active``. A gate left behind by a
    crash is stolen after its TTL (an hour-old gate is a corpse, not a batch).
    """
    uid = int(user_id)
    if user_batch_active(uid):
        return False
    return qj.begin_user_batch(uid, next_batch_id())


def end_user_batch(user_id: int) -> None:
    qj.end_user_batch(int(user_id))


def next_batch_id() -> int:
    """Monotonic id shared by every job of one batch (durable: max + 1)."""
    global _next_batch_id
    with _lock:
        with bot_db.db() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(batch_id), 0) m FROM submit_jobs"
            ).fetchone()
        _next_batch_id = max(_next_batch_id, int(row["m"])) + 1
        return _next_batch_id


def queue_position(job_id: int) -> int:
    """1-based position among global queued+running ordered by id."""
    ids = [int(j["id"]) for j in qj.open_jobs()]
    ids.sort()
    try:
        return ids.index(int(job_id)) + 1
    except ValueError:
        return 0


def open_job_count() -> int:
    return len(qj.open_jobs())


def user_queue_rank(job: QueueJob) -> tuple[int, int]:
    """(1-based index among user's active jobs, total active for user)."""
    jobs = user_active_jobs(job.user_id)
    n = len(jobs)
    for i, j in enumerate(jobs, start=1):
        if j.id == job.id:
            return i, n
    return 0, n


def _stats_block(job: QueueJob, lang: str = "ru") -> list[str]:
    """Human-friendly status counts (only non-zero shown for already/bad/fail)."""
    out = [t("queue.stat_ok", lang, n=job.ok)]
    if job.already:
        out.append(t("queue.stat_already", lang, n=job.already))
    if job.bad:
        out.append(t("queue.stat_bad", lang, n=job.bad))
    if job.fail:
        out.append(t("queue.stat_fail", lang, n=job.fail))
    return out


def job_eta_seconds(job: QueueJob, *, now: datetime | None = None) -> float | None:
    """Remaining seconds from the job's own measured rate, or None.

    Measured, never modelled: projects impose very different per-row delays, so
    a formula would lie. Too early to tell → no number rather than a wrong one.
    """
    if job.status != "running" or job.done <= 0 or job.total <= job.done:
        return None
    started = bot_db.parse_iso(job.started_at)
    if started is None:
        return None
    elapsed = ((now or datetime.now(timezone.utc)) - started).total_seconds()
    if elapsed < 60:
        return None
    rate = job.done / elapsed
    if rate <= 0:
        return None
    return (job.total - job.done) / rate


def format_eta(seconds: float, lang: str = "ru") -> str:
    minutes = max(1, int(seconds // 60))
    if minutes >= 60:
        return t("queue.eta_hm", lang, h=minutes // 60, m=minutes % 60)
    return t("queue.eta_m", lang, m=minutes)


def _slot_free_eta(user_id: int, *, exclude_job_id: int | None = None) -> float | None:
    """When the user's next queue slot frees = the soonest running job ends."""
    etas = []
    for j in user_active_jobs(user_id):
        if j.id == exclude_job_id or j.status != "running":
            continue
        eta = job_eta_seconds(j)
        if eta is None:
            return None
        etas.append(eta)
    return min(etas) if etas else None


def format_job_status(job: QueueJob, *, lang: str = "ru") -> str:
    name = esc(project_display_name(job.project_id))
    my_pos, my_total = user_queue_rank(job)
    # With a parallel cap > 1 several jobs may run; name the one that has to
    # finish before this queued job can start.
    running_jobs = [
        j for j in user_active_jobs(job.user_id) if j.status == "running" and j.id != job.id
    ]
    blocked = len(running_jobs) >= _user_parallel_jobs(job.user_id)
    ahead = running_jobs[0] if (job.status == "queued" and blocked) else None

    # Cancelled / cancelling
    if job.status == "cancelled" or job.cancel_requested:
        lines = [
            f"{t('queue.cancelled_title', lang)} · {t('queue.job_id', lang, id=job.id)}",
            f"<b>{name}</b>",
            t("queue.done_before_stop", lang, done=job.done, total=job.total),
            "",
        ]
        lines.extend(_stats_block(job, lang))
        return "\n".join(lines)

    # Queued (waiting)
    if job.status == "queued":
        lines = [
            f"{t('queue.queued_title', lang)} · {t('queue.job_id', lang, id=job.id)}",
            f"<b>{name}</b>",
            t("queue.accounts", lang, n=job.total),
            t("queue.chain_pos", lang, pos=my_pos, total=my_total),
        ]
        if ahead:
            lines.append(
                t("queue.wait_after", lang, name=esc(project_display_name(ahead.project_id)))
            )
            slot_eta = _slot_free_eta(job.user_id, exclude_job_id=job.id)
            if slot_eta is not None:
                lines.append(t("queue.eta_starts", lang, eta=format_eta(slot_eta, lang)))
        else:
            lines.append(t("queue.wait_worker", lang))
        return "\n".join(lines)

    # Running
    if job.status == "running":
        bar = progress_bar(job.done, job.total)
        left_rows = max(0, job.total - job.done)
        lines = [
            f"{t('queue.running_title', lang)} · {t('queue.job_id', lang, id=job.id)}",
            f"<b>{name}</b>",
            t("queue.bar", lang, bar=bar, done=job.done, total=job.total),
            "",
        ]
        lines.extend(_stats_block(job, lang))
        lines.append(t("queue.left_rows", lang, n=left_rows))
        eta = job_eta_seconds(job)
        if eta is not None:
            lines.append(t("queue.eta", lang, eta=format_eta(eta, lang)))
        # Next queued project in user's chain
        nxt = next(
            (j for j in user_active_jobs(job.user_id) if j.id != job.id),
            None,
        )
        if nxt is not None:
            lines.append(
                t("queue.next_project", lang, name=esc(project_display_name(nxt.project_id)))
            )
        return "\n".join(lines)

    # Done
    if job.status == "done":
        lines = [
            f"{t('queue.done_title', lang)} · {t('queue.job_id', lang, id=job.id)}",
            f"<b>{name}</b>",
            t("queue.accounts", lang, n=job.total),
            "",
        ]
        lines.extend(_stats_block(job, lang))
        if job.skipped_dup:
            lines.append("")
            lines.append(t("queue.skipped_dup_note", lang, n=job.skipped_dup))
        if job.limit_reached:
            lines.append(t("queue.limit_stop_note", lang))
        return "\n".join(lines)

    # Error / unknown — the raw job detail stays in SQLite and the log.
    from bot import errors as bot_errors

    return (
        f"❌ {t('queue.job_id', lang, id=job.id)}\n"
        f"{bot_errors.user_message('FAIL', job.detail or '', lang=lang)}"
    )


def format_user_jobs_summary(user_id: int, *, lang: str = "ru") -> str:
    """Compact single-message dashboard for all active jobs of one user."""
    jobs = user_active_jobs(user_id)
    if not jobs:
        return ""
    has_running = any(j.status == "running" for j in jobs)
    title_key = "queue.active_title" if has_running else "queue.queued_title"
    lines = [
        f"<b>{t(title_key, lang)}</b> · "
        f"{len(jobs)}/{_max_user_jobs(user_id)}"
    ]
    batch = [j for j in jobs if j.batch_id]
    if batch:
        total = max(int(batch[0].batch_total or 0), len(batch))
        lines.append(
            t("queue.batch_progress", lang, done=total - len(batch), total=total)
        )
    shown = jobs[:15]
    for j in shown:
        mark = "🔄" if j.status == "running" else "⏳"
        stats = f" · ✅ {j.ok}"
        if j.already:
            stats += f" · ℹ️ {j.already}"
        if j.bad:
            stats += f" · ⚠️ {j.bad}"
        if j.fail:
            stats += f" · ❌ {j.fail}"
        lines.append(
            f"{mark} {t('queue.job_id', lang, id=j.id)} · "
            f"<b>{esc(project_display_name(j.project_id))}</b> — "
            f"{j.done}/{j.total}{stats}"
        )
    if len(jobs) > len(shown):
        lines.append(t("queue.more_jobs", lang, n=len(jobs) - len(shown)))
    return "\n".join(lines)


def user_progress_keyboard(user_id: int, lang: str = "ru"):
    """Cancel buttons for the shared dashboard.

    One button per job while the user is chaining a few projects by hand; a
    single «stop everything» button once an «all projects» batch is running,
    because twenty per-project buttons are not a keyboard anyone can use.
    """
    jobs = user_active_jobs(user_id)
    if not jobs:
        return None
    live = [j for j in jobs if j.status in ("queued", "running") and not j.cancel_requested]
    if not live:
        return None
    try:
        from telebot import types

        kb = types.InlineKeyboardMarkup()
        if any(j.batch_id for j in live):
            kb.add(types.InlineKeyboardButton(
                t("btn.cancel_all_ask", lang, n=len(live)),
                callback_data="qcancel:all",
            ))
            return kb
        for job in live:
            kb.add(types.InlineKeyboardButton(
                f"⏹ {project_display_name(job.project_id)}",
                callback_data=f"qcancel:ask:{job.id}",
            ))
        return kb if kb.keyboard else None
    except Exception:
        return None


def progress_keyboard(job: QueueJob, lang: str = "ru"):
    """Inline stop button (opens a confirm step) while queued/running."""
    if job.status not in ("queued", "running"):
        return None
    if job.cancel_requested:
        return None
    try:
        from telebot import types

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                t("btn.cancel_ask", lang),
                callback_data=f"qcancel:ask:{job.id}",
            )
        )
        return kb
    except Exception:
        return None


def cancel_confirm_keyboard(job: QueueJob, lang: str = "ru"):
    """Two-step confirm keyboard shown after the user presses «Stop»."""
    try:
        from telebot import types

        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton(
                t("btn.cancel_yes", lang),
                callback_data=f"qcancel:yes:{job.id}",
            ),
            types.InlineKeyboardButton(
                t("btn.cancel_no", lang),
                callback_data=f"qcancel:no:{job.id}",
            ),
        )
        return kb
    except Exception:
        return None


def cancel_all_confirm_keyboard(user_id: int, lang: str = "ru"):
    """Confirm step for «stop everything» (whole batch, one tap to undo)."""
    try:
        from telebot import types

        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton(
                t("btn.cancel_yes", lang),
                callback_data="qcancel:allyes",
            ),
            types.InlineKeyboardButton(
                t("btn.cancel_no", lang),
                callback_data="qcancel:allno",
            ),
        )
        return kb
    except Exception:
        return None


def build_fields_for_project(project: Project, handle: str, wallet: str) -> dict | None:
    """Map handle+wallet (+ auto tweet URLs) into project fields via parse."""
    n_urls = getattr(project, "auto_tweet_urls", None)
    if n_urls is None:
        n_urls = tweet_urls.count_url_fields(project.bulk_columns)
    n_urls = max(0, int(n_urls or 0))
    urls = tweet_urls.generate_status_urls(handle, n_urls)
    # An explicit `auto_tweet_urls = 0` means the route verifies the link
    # a generated snowflake is a proof scheduled to be dropped (measured once).
    # The per-column fallback below must not override that with a synthetic URL.
    auto_urls_off = getattr(project, "auto_tweet_urls", None) is not None and n_urls == 0

    # Prefer bulk_columns order when present
    if project.bulk_columns:
        parts: list[str] = []
        ui = 0
        for key, _req, _lab in project.bulk_columns:
            kl = key.lower()
            if kl in ("handle", "twitter", "x_username", "xhandle", "username"):
                parts.append(handle)
            elif kl == "wallet":
                parts.append(wallet)
            elif tweet_urls.is_url_field_key(key):
                if ui < len(urls):
                    parts.append(urls[ui])
                    ui += 1
                elif not auto_urls_off:
                    parts.append(tweet_urls.generate_status_url(handle))
                else:
                    parts.append("")
            else:
                parts.append("")
        # drop trailing empties for parse flexibility
        while parts and parts[-1] == "":
            parts.pop()
        line = "|".join(parts)
        fields = project.parse(line)
        if fields:
            return fields
        # wallet-only + url projects sometimes want wallet|url only
        if len(parts) >= 2:
            fields = project.parse(f"{wallet}|{parts[-1]}")
            if fields:
                return fields

    # Fallbacks
    for candidate in (
        f"{handle}|{wallet}",
        f"{handle},{wallet}",
        f"{wallet}|{urls[0]}" if urls else wallet,
        wallet,
    ):
        fields = project.parse(candidate)
        if fields:
            return fields
    return None


def _self_sourced_rows(
    user_id: int, project: Project, slots: int
) -> tuple[list[dict], list[str], int]:
    """Rows for a project that mints its own wallets.

    No /data, and no global wallet dedup: the addresses are brand new, so the
    lookup would only cost a large query. ``batch_size`` is all-or-nothing —
    a half batch is not worth a user's slots.
    """
    want = project.batch_size or slots
    if slots <= 0:
        return [], [f"нет свободных слотов по проекту {project.name}"], 0
    if slots < want:
        return [], [f"нужно {want} свободных слотов, осталось {slots}"], 0

    try:
        rows = project.generate_rows(user_id=user_id, count=want)
    except Exception as exc:
        return [], [f"не удалось создать кошельки: {exc}"], 0

    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return [], ["проект не создал ни одной строки"], 0
    return rows[:want], [], 0


def prepare_job_rows(
    user_id: int, project: Project
) -> tuple[list[dict], list[str], int]:
    """
    Returns (fields_list, error_messages, skipped_dup_count).

    Skips wallets that already have OK/ALREADY for this user+project in bot DB.

    Self-sourced projects take the other branch: they mint their own rows and
    never touch /data (see ``_self_sourced_rows``).
    """
    lim = None
    try:
        lim = bot_limits.effective_ok_limit(user_id, fresh=True)
    except Exception:
        lim = 10
    ok_count = bot_db.get_ok_count(user_id, project.id)
    slots = remaining(ok_count, lim)

    if getattr(project, "self_sourced", False):
        return _self_sourced_rows(user_id, project, slots)

    data_rows = user_data_mod.load_user_data_rows(user_id)
    if not data_rows:
        return [], ["нет данных — загрузи /data .txt (handle|wallet)"], 0

    # Global: any TG user already OK/ALREADY on this project+wallet
    known = bot_db.wallets_already_registered(
        project.id,
        [r.wallet for r in data_rows],
    )

    prepared: list[dict] = []
    errors: list[str] = []
    skipped_dup = 0
    chain_id = uuid.uuid4().hex
    for i, row in enumerate(data_rows, start=1):
        if len(prepared) >= slots:
            break
        w = (row.wallet or "").strip().lower()
        if w in known:
            skipped_dup += 1
            continue
        fields = build_fields_for_project(project, row.handle, row.wallet)
        if not fields:
            errors.append(f"стр. {i}: не собрались поля для {project.id}")
            continue
        err = project.validate(fields)
        if err:
            errors.append(f"стр. {i}: {err}")
            continue
        queue_prepare = getattr(project, "queue_prepare", None)
        if queue_prepare is not None:
            try:
                queue_prepare(
                    fields,
                    user_id=user_id,
                    sequence=len(prepared),
                    chain_id=chain_id,
                )
            except Exception as exc:
                errors.append(f"стр. {i}: queue setup failed: {exc}")
                continue
        prepared.append(fields)

    if not prepared and not errors and skipped_dup:
        errors.append(
            f"все {skipped_dup} кошельков уже были OK/ALREADY в боте "
            f"(глобально) по этому проекту"
        )
    elif not prepared and not errors:
        errors.append("нет слотов или нет валидных строк")
    return prepared, errors, skipped_dup


def enqueue(
    *,
    user_id: int,
    project: Project,
    chat_id: int,
    message_id: int | None,
    rows: list[dict],
    skipped_dup: int = 0,
    batch_id: int = 0,
    batch_total: int = 0,
) -> QueueJob:
    """Persist a queued job + its rows in one transaction (no payload files).

    Ungated — callers that need the duplicate/cap/disable gates must use
    :func:`enqueue_if_allowed`.
    """
    job_id = qj.create_job(
        user_id=int(user_id),
        project_id=project.id,
        chat_id=int(chat_id),
        message_id=message_id,
        rows=rows,
        skipped_dup=skipped_dup,
        batch_id=batch_id,
        batch_total=batch_total,
        parallel=resolve_job_parallel(project, int(user_id)),
    )
    row = qj.load_job(job_id)
    job = _row_to_job(row or {"id": job_id, "user_id": int(user_id),
                              "project_id": project.id, "status": "queued",
                              "chat_id": int(chat_id), "total": len(rows)})
    with _lock:
        _jobs[job_id] = job
    log(
        f"[queue] enqueue job={job_id} user={user_id} project={project.id} "
        f"n={len(rows)} skip_dup={skipped_dup}"
    )
    return job


def enqueue_if_allowed(
    *,
    user_id: int,
    project: Project,
    chat_id: int,
    message_id: int | None,
    rows: list[dict],
    skipped_dup: int = 0,
    batch_id: int = 0,
    batch_total: int = 0,
    auto_reg: bool = False,
) -> tuple[QueueJob | None, str, QueueJob | None]:
    """
    Atomic gate + insert — in SQLite, not under a Python lock.

    The checks (runtime-disable, same-project duplicate, per-user cap) and
    the job+rows insert run inside one ``BEGIN IMMEDIATE``
    (``qj.create_job_if_allowed``), so two writer processes are as safe as
    two threads used to be. The partial unique index on
    (user_id, project_id) WHERE status IN ('queued','running') is the
    backstop.

    Returns ``(job_or_none, reason, existing_job_or_none)``. ``reason`` is one
    of: ``""`` (ok), ``"duplicate"``, ``"full"``, ``"disabled"``.
    """
    uid = int(user_id)
    cap_auto_reg = bool(auto_reg)
    job_id, reason = qj.create_job_if_allowed(
        user_id=uid,
        project_id=project.id,
        chat_id=int(chat_id),
        message_id=message_id,
        rows=rows,
        skipped_dup=skipped_dup,
        batch_id=batch_id,
        batch_total=batch_total,
        auto_reg=cap_auto_reg,
        parallel=resolve_job_parallel(project, uid),
        max_user_jobs_fn=lambda u: _max_user_jobs(u, auto_reg=cap_auto_reg),
        project_enabled_fn=lambda pid: not bot_db.is_project_runtime_disabled(pid),
    )
    if job_id is None:
        existing = user_has_project_job(uid, project.id) if reason == "duplicate" else None
        return None, reason, existing
    row = qj.load_job(job_id) or {}
    job = _row_to_job(row)
    with _lock:
        _jobs[job_id] = job
    log(
        f"[queue] enqueue job={job_id} user={uid} project={project.id} "
        f"n={len(rows)} skip_dup={skipped_dup}"
    )
    return job, "", None


def job_view(job: QueueJob, lang: str = "ru"):
    """Text + markup for this user's queue, as a screen View."""
    from telebot import types

    from bot import privatka

    active = user_active_jobs(job.user_id)
    text = (
        format_user_jobs_summary(job.user_id, lang=lang)
        if active
        else format_job_status(job, lang=lang)
    )
    markup = (
        user_progress_keyboard(job.user_id, lang=lang)
        if active
        else progress_keyboard(job, lang=lang)
    )
    text, markup = privatka.attach_after_job(text, markup, job)
    # Empty markup clears the cancel button on done/cancelled.
    return screen.View(text, markup if markup is not None else types.InlineKeyboardMarkup())


def _remember_message(job: QueueJob, message_id: int) -> None:
    """Point this user's whole chain at a (new) dashboard message."""
    with _lock:
        for j in _jobs.values():
            if j.user_id == job.user_id and j.status in ("queued", "running"):
                j.message_id = message_id
        job.message_id = message_id
    try:
        with bot_db.db() as conn:
            conn.execute(
                "UPDATE submit_jobs SET message_id = ? "
                "WHERE user_id = ? AND status IN ('queued','running')",
                (int(message_id), int(job.user_id)),
            )
            conn.commit()
    except Exception as exc:
        log(f"[queue] remember message job={job.id}: {exc}")


def update_job_message(bot, job: QueueJob, lang: str = "ru") -> None:
    """Redraw queue progress on the chain's own dashboard message.

    The dashboard is deliberately NOT the navigation anchor: tying them
    together meant starting a job ate whatever screen the user was on, and
    browsing anywhere else froze the progress counter. One message for the
    chain, one for navigation, neither steps on the other.
    """
    if not bot or not job.chat_id:
        return
    active = user_active_jobs(job.user_id)
    # Chained projects share the first job's message.
    dashboard = next((j for j in active if j.message_id), None)
    message_id = dashboard.message_id if dashboard else job.message_id
    view = job_view(job, lang=lang)
    if message_id:
        try:
            bot.edit_message_text(
                view.text,
                chat_id=job.chat_id,
                message_id=message_id,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=view.markup,
            )
            return
        except Exception as exc:
            desc = str(getattr(exc, "description", "") or exc).lower()
            if "message is not modified" in desc:
                return
            gone = any(
                k in desc
                for k in (
                    "message to edit not found",
                    "message can't be edited",
                    "message_id_invalid",
                )
            )
            if not gone:
                # Transient (rate limit, network): the next tick redraws.
                return
    # No dashboard yet, or the user deleted it — post one and remember it.
    try:
        msg = bot.send_message(
            job.chat_id,
            view.text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=view.markup,
        )
    except Exception as exc:
        log(f"[queue] dashboard send job={job.id}: {exc}")
        return
    _remember_message(job, int(msg.message_id))


def cancel_jobs_for_project(
    project_id: str,
    *,
    detail: str = "project disabled by admin",
    bot=None,
) -> int:
    """
    Soft-cancel all queued/running jobs for project_id (admin form-closed).

    Queued → cancelled immediately (rows dropped, in the same transaction);
    running → cancel_requested between rows. Returns number of jobs touched.
    """
    pid = (project_id or "").strip()
    if not pid:
        return 0
    detail = (detail or "project disabled by admin")[:200]
    touched = 0
    for row in qj.open_jobs():
        if str(row["project_id"]) != pid:
            continue
        job = _cached_or_row(row)
        if qj.request_cancel(int(row["id"]), detail=detail):
            touched += 1
            log(
                f"[queue] admin cancel job={job.id} project={pid} "
                f"user={job.user_id}"
            )
        if bot is not None:
            try:
                lang = bot_db.get_lang(job.user_id)
            except Exception:
                lang = "ru"
            try:
                update_job_message(bot, job, lang=lang)
            except Exception:
                pass
        if job.status != "running":
            # Queued: it is finished now — release project-owned state.
            _cleanup_project_job(job)
            _purge_job_soon(job.id)
    return touched


def cancel_jobs_without_own_proxies(
    *,
    bot=None,
    detail: str = "shared proxy pool off",
) -> int:
    """Soft-cancel queued/running jobs whose user has no own /proxy file.

    Used when the shared pool is taken offline so those rows do not sit in
    the queue burning FAIL/no_proxies. Users with own proxies keep running.
    """
    from bot.user_proxies import has_own_proxies

    detail = (detail or "shared proxy pool off")[:200]
    touched = 0
    for row in qj.open_jobs():
        uid = int(row["user_id"])
        if has_own_proxies(uid):
            continue
        job = _cached_or_row(row)
        if qj.request_cancel(int(row["id"]), detail=detail):
            touched += 1
            log(
                f"[queue] shared-off cancel job={job.id} "
                f"user={uid} project={job.project_id}"
            )
        if bot is not None:
            try:
                lang = bot_db.get_lang(uid)
            except Exception:
                lang = "ru"
            try:
                update_job_message(bot, job, lang=lang)
            except Exception:
                pass
        if job.status != "running":
            _cleanup_project_job(job)
            _purge_job_soon(job.id)
    return touched


def cancel_jobs_for_user(
    user_id: int,
    *,
    detail: str = "user banned by admin",
    bot=None,
) -> int:
    """Cancel all queued jobs and soft-stop running jobs for one user."""
    uid = int(user_id)
    detail = (detail or "user banned by admin")[:200]
    touched = 0
    for row in qj.open_jobs(uid):
        job = _cached_or_row(row)
        if qj.request_cancel(int(row["id"]), detail=detail):
            touched += 1
        if job.status != "running":
            _cleanup_project_job(job)
            _purge_job_soon(job.id)
        if bot is not None:
            try:
                update_job_message(bot, job, lang=bot_db.get_lang(uid))
            except Exception:
                pass
    return touched


def cancel_user_job(user_id: int, *, job_id: int | None = None) -> tuple[bool, str, QueueJob | None]:
    """
    Cancel the caller's job (queued or running).

    Without job_id: cancel running first, else first queued.
    Returns (ok, reason, job). Queued jobs die in the same SQLite
    transaction that flags them; running ones stop between rows.
    """
    uid = int(user_id)
    if job_id is not None:
        job = None
        for j in user_active_jobs(uid):
            if j.id == int(job_id):
                job = j
                break
        if job is None:
            return False, "job not found or not yours", None
    else:
        job = user_has_active_job(uid)
        if job is None:
            return False, "no active job", None
    if job.status not in ("queued", "running"):
        return False, f"job already {job.status}", job
    if job.cancel_requested and job.status == "running":
        return True, "already cancelling", job

    if not qj.request_cancel(job.id, detail="cancelled by user"):
        return False, f"job already {job.status}", job
    job.cancel_requested = True
    if job.status == "queued":
        job.status = "cancelled"
        job.detail = "cancelled by user"
        job.finished_at = _now()
        _cleanup_project_job(job)
        _purge_job_soon(job.id)
        log(f"[queue] cancel queued job={job.id} user={uid}")
        return True, "cancelled", job
    job.detail = "cancel requested"
    log(
        f"[queue] cancel requested job={job.id} user={uid} "
        f"done={job.done}/{job.total}"
    )
    return True, "cancelling", job


def cancel_all_user_jobs(user_id: int) -> tuple[int, QueueJob | None]:
    """Cancel every queued/running job of one user (batch stop).

    Returns (how many jobs were touched, one job to redraw the dashboard from).
    Queued jobs die immediately, running ones stop between rows — same
    semantics as a single cancel, just applied to the whole chain.
    """
    uid = int(user_id)
    touched = 0
    last: QueueJob | None = None
    for job in user_active_jobs(uid):
        ok, _reason, cancelled = cancel_user_job(uid, job_id=job.id)
        if ok and cancelled is not None:
            touched += 1
            last = cancelled
    if last is None:
        last = user_has_active_job(uid)
    return touched, last


def _purge_job_soon(job_id: int, delay_sec: float = 120.0) -> None:
    """Drop finished job from memory after a short delay (keep /queue readable)."""

    def _run() -> None:
        time.sleep(max(5.0, delay_sec))
        with _lock:
            j = _jobs.get(int(job_id))
            if j and j.status in ("done", "cancelled", "error"):
                _jobs.pop(int(job_id), None)

    threading.Thread(
        target=_run, name=f"purge-job-{job_id}", daemon=True
    ).start()


def _row_get(r, key: str, idx: int):
    if hasattr(r, "keys"):
        return r[key]
    return r[idx]


def _boot_settle_legacy_payload_jobs(bot=None) -> int:
    """One-time settlement for jobs created before the lease protocol.

    Jobs whose rows never made it into submit_job_rows still carry the old
    {id}.json payload sidecar. On boot they are either re-enqueued through
    the same gates as a manual start (BOOT_RESUME_JOBS=1) or marked
    ``bot_restart``; either way the sidecar file goes away, so after one
    boot past the cutover the payload directory is empty for good.
    """
    resume = bool(getattr(config, "BOOT_RESUME_JOBS", False))
    settled = 0
    for path in sorted(_payload_dir().glob("*.json")):
        try:
            jid = int(path.stem)
        except ValueError:
            continue
        row = None
        with bot_db.db() as conn:
            r = conn.execute(
                "SELECT * FROM submit_jobs WHERE id=?", (jid,)
            ).fetchone()
            row = dict(r) if r else None
            if row is not None and row.get("status") in ("queued", "running"):
                # Interrupted pre-cutover job; settle it so the lease queue
                # does not see a half-old row.
                conn.execute(
                    "UPDATE submit_jobs SET status='error', detail=?, "
                    "finished_at=? WHERE id=?",
                    ("bot_restart", _now(), jid),
                )
                conn.commit()
        try:
            job_rows: list[dict] = []
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                job_rows = [x for x in data if isinstance(x, dict)]
        except Exception as exc:
            log(f"[queue] read legacy payload job={jid}: {exc}")
            job_rows = []
        pid = str((row or {}).get("project_id") or "")
        if job_rows and pid:
            try:
                bot_db.release_wallet_claims(
                    pid, [str(r.get("wallet") or "") for r in job_rows]
                )
            except Exception as exc:
                log(f"[queue] release legacy claims job={jid}: {exc}")
        if resume and job_rows and row is not None:
            project = get_project(pid)
            if project is not None:
                try:
                    resumed, reason, _ = enqueue_if_allowed(
                        user_id=int(row["user_id"]),
                        project=project,
                        chat_id=int(row.get("chat_id") or 0),
                        message_id=(int(row["message_id"])
                                    if row.get("message_id") else None),
                        rows=job_rows,
                    )
                    if resumed is not None and bot is not None:
                        update_job_message(
                            bot, resumed, lang=bot_db.get_lang(int(row["user_id"]))
                        )
                except Exception as exc:
                    log(f"[queue] legacy resume job={jid} failed: {exc}")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        settled += 1
    return settled


def reconcile_zombie_jobs_on_boot(bot=None) -> int:
    """Boot settlement under the lease protocol.

    Lease-protocol jobs need no replay: an interrupted `running` job's lease
    expires within LEASE_SECONDS, the next claim's sweep requeues it with its
    running rows back to pending, and a worker resumes it. Queued jobs are
    already claimable. What boot still owes:

    * release in-flight wallet claims of interrupted running jobs (their
      rows go back to pending and will re-run; the global dedup turns any
      landed duplicate into ALREADY);
    * clear stale OK-slot reservations (their holders are gone);
    * settle pre-cutover payload-file jobs (see _boot_settle_legacy_payload_jobs).
    """
    try:
        cleared_slots = bot_db.clear_ok_slot_reservations()
    except Exception as exc:
        cleared_slots = 0
        log(f"[queue] clear boot slot reservations: {exc}")

    interrupted = 0
    for row in qj.open_jobs():
        if str(row["status"]) != "running":
            continue
        jid = int(row["id"])
        interrupted += 1
        rows = qj.job_rows(jid)
        if rows:
            try:
                bot_db.release_wallet_claims(
                    str(row["project_id"]),
                    [str(r.get("wallet") or "") for _seq, r in rows],
                )
            except Exception as exc:
                log(f"[queue] release interrupted claims job={jid}: {exc}")
        log(
            f"[queue] interrupted job={jid} left leased; the sweep will "
            f"requeue it within {int(LEASE_SECONDS)}s"
        )

    legacy = _boot_settle_legacy_payload_jobs(bot)
    if interrupted or cleared_slots or legacy:
        log(
            f"[queue] boot reconcile: interrupted_running={interrupted} "
            f"legacy_payload={legacy} slot_reservations={cleared_slots}"
        )
    return interrupted + legacy


def _persist_progress(job: QueueJob) -> None:
    """Fenced counters write; a lost lease makes this a no-op (job.status in
    memory still moves so the dashboard keeps rendering what we know)."""
    if job.owner:
        qj.update_progress(
            job.id, job.owner, job.generation,
            total=job.total, done=job.done, ok=job.ok, fail=job.fail,
            already=job.already, bad=job.bad,
            message_id=job.message_id,
        )
    else:
        with bot_db.db() as conn:
            conn.execute(
                "UPDATE submit_jobs SET total=?, done=?, ok=?, fail=?, "
                "already=?, bad=?, message_id=? WHERE id=?",
                (job.total, job.done, job.ok, job.fail, job.already,
                 job.bad, job.message_id, job.id),
            )
            conn.commit()


# Back-compat alias: the old name persists status+counters; callers in
# tests still use it for terminal states.
def _set_status(job: QueueJob, status: str, **fields: Any) -> None:
    job.status = status
    for k, v in fields.items():
        setattr(job, k, v)
    if status in ("done", "cancelled", "error"):
        if job.owner:
            qj.finish_job(
                job.id, job.owner, job.generation, status,
                detail=job.detail or "",
                counters={"total": job.total, "done": job.done, "ok": job.ok,
                          "fail": job.fail, "already": job.already,
                          "bad": job.bad},
            )
        else:
            with bot_db.db() as conn:
                conn.execute(
                    "UPDATE submit_jobs SET status=?, detail=?, finished_at=? "
                    "WHERE id=?",
                    (status, (job.detail or "")[:500],
                     job.finished_at or _now(), job.id),
                )
                conn.commit()
    else:
        _persist_progress(job)


def _apply_row_result(job: QueueJob, result: dict | None) -> None:
    """Count only real work. SKIPPED/STOPPED never inflate bad/fail (AUD-006)."""
    st = (result or {}).get("status", "FAIL")
    if st in ("SKIPPED", "STOPPED") or (result or {}).get("skipped"):
        return
    job.done += 1
    if st == "OK":
        job.ok += 1
    elif st == "ALREADY":
        job.already += 1
    elif st == "BAD_RESP":
        job.bad += 1
    else:
        job.fail += 1


def _note_own_proxy_fail(
    job: QueueJob,
    result: dict | None,
    *,
    lang: str,
) -> None:
    """After 20 consecutive own-proxy transport FAILs, DM the user once.

    Shared-pool failures stay silent. Any OK / ALREADY / BAD_RESP / non-proxy
    FAIL resets the streak. SKIPPED rows never reach here.
    """
    if not job.uses_own_proxies or job.own_proxy_alert_sent or _bot is None:
        return
    st = (result or {}).get("status", "")
    if st in ("SKIPPED", "STOPPED") or (result or {}).get("skipped"):
        return
    detail = str((result or {}).get("detail") or "")
    if st == "FAIL" and is_proxy_fail_detail(detail):
        job.own_proxy_fail_streak += 1
        if job.own_proxy_fail_streak < OWN_PROXY_FAIL_ALERT_STREAK:
            return
        job.own_proxy_alert_sent = True
        try:
            safe_send(_bot, job.chat_id, t("queue.own_proxy_dead", lang))
        except Exception as exc:
            log(f"[queue] own-proxy alert job={job.id} user={job.user_id}: {exc}")
        return
    if st in ("OK", "ALREADY", "BAD_RESP", "FAIL", "BUSY"):
        job.own_proxy_fail_streak = 0


class _LeaseLost(Exception):
    """Raised internally when our lease was reclaimed mid-job."""


def _heartbeat(job: QueueJob, stop_flag: threading.Event) -> None:
    """Renew the lease every LEASE_SECONDS/3 while the job runs."""
    interval = max(5.0, LEASE_SECONDS / 3.0)
    while not stop_flag.wait(interval):
        try:
            if not qj.renew_lease(job.id, job.owner, job.generation, LEASE_SECONDS):
                log(f"[queue] lease lost job={job.id} owner={job.owner}")
                stop_flag.set()
                return
        except Exception as exc:  # noqa: BLE001 — never kill the heartbeat
            log(f"[queue] heartbeat job={job.id}: {exc}")


def _process_job(job: QueueJob) -> None:
    """Run one claimed job to its terminal state.

    The caller (the worker loop) hands over a job claimed from SQLite with
    ``owner``/``generation`` set; every DB write below is fenced by them. A
    lost lease raises/stops the row loop and leaves the job running-leased —
    the new owner's sweep decides its fate; we do NOT finish it.
    """
    assert _submit_fn is not None and _bot is not None
    project = get_project(job.project_id)
    if project is None:
        job.detail = "project unavailable"
        job.finished_at = _now()
        _set_status(job, "error", finished_at=job.finished_at, detail=job.detail)
        update_job_message(_bot, job)
        _cleanup_project_job(job)
        _purge_job_soon(job.id)
        return

    # Only the unfinished rows: a job reclaimed after a dead lease resumes
    # here — rows the previous owner completed are not re-taken (their
    # mark_row_running fence would read as a lost lease).
    seq_rows = qj.pending_rows(job.id)
    job.rows = [r for _s, r in seq_rows]
    job.total = max(job.total, len(seq_rows))
    if not seq_rows and job.total and int((qj.load_job(job.id) or {}).get("done") or 0) >= job.total:
        # Everything already landed under a previous owner; just finish.
        job.finished_at = _now()
        _set_status(job, "done", finished_at=job.finished_at, detail=job.detail)
        update_job_message(_bot, job, lang=bot_db.get_lang(job.user_id))
        _deliver_project_job(job)
        _cleanup_project_job(job)
        _purge_job_soon(job.id)
        return

    job.parallel = resolve_job_parallel(project, job.user_id)

    try:
        from bot import user_proxies as user_proxies_mod

        job.uses_own_proxies = bool(user_proxies_mod.has_own_proxies(job.user_id))
    except Exception:
        job.uses_own_proxies = False
    job.own_proxy_fail_streak = 0
    job.own_proxy_alert_sent = False

    job.started_at = _now()
    job.status = "running"
    _persist_progress(job)
    update_job_message(_bot, job)

    lang = "ru"
    try:
        lang = bot_db.get_lang(job.user_id)
    except Exception:
        pass

    hb_stop = threading.Event()
    hb = threading.Thread(
        target=_heartbeat, args=(job, hb_stop), name=f"lease-hb-{job.id}",
        daemon=True,
    )
    hb.start()

    last_edit = 0.0
    edit_lock = threading.Lock()

    def _maybe_edit(force: bool = False) -> None:
        nonlocal last_edit
        with edit_lock:
            now = time.monotonic()
            if (
                force
                or now - last_edit >= 2.0
                or job.done == job.total
                or job.done == 1
            ):
                _persist_progress(job)
                update_job_message(_bot, job, lang=lang)
                last_edit = now
            elif job.done % 5 == 0:
                _persist_progress(job)

    def _limit_hit() -> bool:
        try:
            lim = bot_limits.effective_ok_limit(job.user_id, fresh=True)
            return bot_db.get_ok_count(job.user_id, job.project_id) >= lim
        except Exception:
            return False

    def _row_outcome(seq: int, result: dict) -> None:
        """Persist one row's outcome; a fenced no-op after a lost lease."""
        try:
            qj.mark_row_done(
                job.id, seq, job.owner, job.generation,
                status="done",
                error=str((result or {}).get("detail") or "")[:200],
            )
        except Exception as exc:  # noqa: BLE001
            log(f"[queue] row done job={job.id} seq={seq}: {exc}")

    def _one(seq: int, fields: dict) -> dict:
        # Fence BEFORE the HTTP call: a False here means the lease was lost
        # and another worker owns this row now — do not send anything.
        if job.owner:
            if not qj.mark_row_running(job.id, seq, job.owner, job.generation):
                raise _LeaseLost()
        if job.cancel_requested or job.limit_reached or _stop.is_set():
            # AUD-006: not a real BAD_RESP — was never submitted
            return {"status": "SKIPPED", "detail": "job_stopped", "skipped": True}
        if bot_db.is_banned(job.user_id):
            job.cancel_requested = True
            job.detail = "user banned by admin"
            return {"status": "SKIPPED", "detail": "user_banned", "skipped": True}
        if _limit_hit():
            job.limit_reached = True
            return {
                "status": "SKIPPED",
                "detail": "ok_limit_reached",
                "skipped": True,
            }
        wallet = (fields.get("wallet") or "").strip().lower()
        if wallet:
            prev = bot_db.already_registered_wallet(job.project_id, wallet)
            if prev:
                # Count as already without HTTP / without new DB row
                return {
                    "status": "ALREADY",
                    "detail": "bot_dedup_global",
                    "wallet": wallet,
                    "skip_record": True,
                }
        try:
            result = _submit_fn(project, fields, job.user_id) or {}
            if result.get("limit_reached") or result.get("detail") == "ok_limit_reached":
                job.limit_reached = True
            return result
        except Exception as e:
            return {"status": "FAIL", "detail": str(e)}

    lease_lost = False
    try:
        if job.parallel <= 1:
            for seq, fields in seq_rows:
                if _stop.is_set() or job.cancel_requested or job.limit_reached:
                    break
                try:
                    result = _one(seq, fields)
                except _LeaseLost:
                    lease_lost = True
                    break
                # skip_record ALREADY still increments job.already for UX
                _apply_row_result(job, result)
                _note_own_proxy_fail(job, result, lang=lang)
                _row_outcome(seq, result)
                if job.cancel_requested or job.limit_reached:
                    break
                _maybe_edit()
        else:
            idx = 0
            in_flight: set = set()
            counter_lock = threading.Lock()
            with ThreadPoolExecutor(
                max_workers=job.parallel, thread_name_prefix=f"job{job.id}"
            ) as pool:
                while idx < len(seq_rows) or in_flight:
                    if _stop.is_set() or job.cancel_requested or job.limit_reached:
                        break
                    while (
                        idx < len(seq_rows)
                        and len(in_flight) < job.parallel
                        and not job.cancel_requested
                        and not job.limit_reached
                        and not _stop.is_set()
                    ):
                        fut = pool.submit(_one, seq_rows[idx][0], seq_rows[idx][1])
                        in_flight.add(fut)
                        idx += 1
                    if not in_flight:
                        break
                    done_futs = []
                    for fut in list(in_flight):
                        if fut.done():
                            done_futs.append(fut)
                    if not done_futs:
                        time.sleep(0.05)
                        continue
                    for fut in done_futs:
                        in_flight.discard(fut)
                        try:
                            result = fut.result()
                        except _LeaseLost:
                            lease_lost = True
                            break
                        except Exception as e:
                            result = {"status": "FAIL", "detail": str(e)}
                        with counter_lock:
                            _apply_row_result(job, result)
                            _note_own_proxy_fail(job, result, lang=lang)
                        _maybe_edit()
                    if lease_lost:
                        break
                if not lease_lost:
                    for fut in list(in_flight):
                        try:
                            # Requests cannot be force-cancelled safely. The
                            # executor waits for them on context exit anyway,
                            # so count their real result instead of recording
                            # a timeout and later an OK.
                            result = fut.result()
                        except _LeaseLost:
                            lease_lost = True
                        except Exception as e:
                            result = {"status": "FAIL", "detail": str(e)}
                        with counter_lock:
                            _apply_row_result(job, result)
                            _note_own_proxy_fail(job, result, lang=lang)
                    _maybe_edit(force=True)
    finally:
        hb_stop.set()

    if lease_lost:
        # Our writes were fenced out; the new owner owns the job's fate.
        # Persist nothing terminal — leave the row loop to whoever reclaimed.
        log(
            f"[queue] lease lost mid-job job={job.id} gen={job.generation} "
            f"done={job.done}/{job.total}"
        )
        _purge_job_soon(job.id)
        return

    job.finished_at = _now()
    if _stop.is_set() and not job.cancel_requested:
        # Process/service shutdown is not a user cancellation. Leave the job
        # running-leased: the lease expires, the sweep requeues it with the
        # unfinished rows back to pending, and the next boot resumes it.
        log(
            f"[queue] interrupted for restart job={job.id} "
            f"ok={job.ok} already={job.already} bad={job.bad} fail={job.fail}"
        )
        update_job_message(_bot, job, lang=lang)
        _cleanup_project_job(job)
        _purge_job_soon(job.id)
        return
    if job.cancel_requested:
        if not job.detail or job.detail == "cancel requested":
            job.detail = "cancelled by user"
        _set_status(
            job,
            "cancelled",
            finished_at=job.finished_at,
            detail=job.detail,
        )
        log(
            f"[queue] cancelled job={job.id} ok={job.ok} already={job.already} "
            f"bad={job.bad} fail={job.fail} done={job.done}/{job.total}"
        )
    else:
        if job.limit_reached:
            job.detail = "ok_limit_reached"
        _set_status(job, "done", finished_at=job.finished_at, detail=job.detail)
        log(
            f"[queue] done job={job.id} ok={job.ok} already={job.already} "
            f"bad={job.bad} fail={job.fail} par={job.parallel} "
            f"limit_hit={job.limit_reached}"
        )
    update_job_message(_bot, job, lang=lang)
    _maybe_send_auto_reg_dm(job, lang=lang)
    _deliver_project_job(job)
    _cleanup_project_job(job)
    _purge_job_soon(job.id)


def _maybe_send_auto_reg_dm(job: QueueJob, *, lang: str = "ru") -> None:
    """One personal DM when an auto-registration run ends (not a broadcast)."""
    if not getattr(job, "auto_reg", False):
        return
    try:
        from bot import auto_reg

        if not job.project_name:
            project = get_project(job.project_id, allow_runtime_disabled=True)
            job.project_name = str(getattr(project, "name", "") or job.project_id)
        text = auto_reg.finish_message(job, lang=lang)
        from bot import privatka

        text, markup = privatka.attach_after_job(text, None, job)
        _bot_send_quiet(job.user_id, text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:  # noqa: BLE001 — the DM must never fail the job
        log(f"[autoreg] finish DM failed job={job.id}: {exc}")


def _bot_send_quiet(
    chat_id: int, text: str, *, parse_mode: str = "", reply_markup=None
) -> None:
    """Best-effort send; a user who blocked the bot is flagged, not retried."""
    if _bot is None:
        return
    try:
        kwargs = {}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        _bot.send_message(chat_id, text, **kwargs)
    except Exception as exc:  # noqa: BLE001
        low = str(exc).lower()
        if "blocked" in low or "forbidden" in low or "user is deactivated" in low:
            try:
                from bot import db as bot_db

                bot_db.set_bot_blocked(int(chat_id))
            except Exception:
                pass
        log(f"[queue] dm to {chat_id} failed: {exc}")


def _user_parallel_jobs(user_id: int) -> int:
    """How many jobs this user may run at once (tier perk; default USER_PARALLEL_JOBS)."""
    try:
        return max(1, int(bot_limits.parallel_jobs(user_id)))
    except Exception:
        return 1


def _project_claimable(project_id: str) -> bool:
    """Claim gate: the registry knows it and the admin has not disabled it."""
    if bot_db.is_project_runtime_disabled(project_id):
        return False
    return get_project(project_id) is not None


def _worker_loop(worker_id: int) -> None:
    owner = _worker_owner(worker_id)
    log(f"[queue] worker-{worker_id} started owner={owner}")
    while not _stop.is_set():
        try:
            claimed = qj.claim_next_job(
                owner,
                LEASE_SECONDS,
                user_parallel_fn=_user_parallel_jobs,
                project_limit_fn=_project_parallel_limit,
                project_claimable_fn=_project_claimable,
            )
        except Exception as e:  # noqa: BLE001 — a claim error must not kill a worker
            log(f"[queue] worker-{worker_id} claim ERR {e}")
            time.sleep(1.0)
            continue
        if claimed is None:
            time.sleep(0.25)
            continue
        job = _row_to_job(claimed)
        job.owner = owner
        job.generation = int(claimed["attempts"])
        with _lock:
            _jobs[job.id] = job
        try:
            _process_job(job)
        except Exception as e:
            log(f"[queue] worker-{worker_id} job={job.id} ERR {e}")
            job.detail = str(e)
            job.finished_at = _now()
            _set_status(job, "error", finished_at=job.finished_at, detail=job.detail)
            try:
                update_job_message(_bot, job)
            except Exception:  # noqa: BLE001
                pass
            # Deliver runs too: rows settled before the crash are real and
            # the user paid slots for them.
            _deliver_project_job(job)
            _cleanup_project_job(job)
            _purge_job_soon(job.id)
    log(f"[queue] worker-{worker_id} stop")


def start_workers(bot, submit_fn: SubmitFn, *, n_workers: int | None = None) -> None:
    global _workers_started, _bot, _submit_fn
    if _workers_started:
        return
    init_schema()
    _bot = bot
    _submit_fn = submit_fn
    try:
        reconcile_zombie_jobs_on_boot(bot)
    except Exception as e:
        log(f"[queue] boot reconcile failed: {e}")
    n = n_workers if n_workers is not None else max(1, int(config.BOT_CONCURRENCY))
    _stop.clear()
    for i in range(n):
        t = threading.Thread(
            target=_worker_loop,
            args=(i + 1,),
            name=f"submit-queue-{i+1}",
            daemon=True,
        )
        t.start()
    _workers_started = True
    log(f"[queue] started {n} workers")


def get_job(job_id: int) -> QueueJob | None:
    with _lock:
        live = _jobs.get(int(job_id))
    if live is not None:
        return live
    row = qj.load_job(int(job_id))
    return _row_to_job(row) if row else None


def reset_runtime_state() -> None:
    """Drop the render cache and every open job (tests and full resets)."""
    with _lock:
        _jobs.clear()
    with bot_db.db() as conn:
        conn.execute("DELETE FROM submit_job_rows")
        conn.execute(
            "DELETE FROM submit_jobs WHERE status IN ('queued','running')"
        )
        conn.execute("DELETE FROM user_batch_gates")
        conn.commit()
