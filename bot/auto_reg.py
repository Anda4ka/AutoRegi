"""Auto-registration: a new project appears → queue a run for opted-in users.

Contract (operator, 2026-09-08):

* **Opt-in only.** ``users.auto_reg``, off by default. Nobody's proxies or
  captcha balance are spent without their explicit switch.
* **Everything goes through the normal queue gates.** Rows are
  ``prepare_job_rows`` + ``enqueue_if_allowed`` per user: tier limits, slot
  reservation, global wallet dedup, duplicate-job and runtime-disable checks
  all still apply. The per-user *queue depth* is raised to the number of
  live projects (same as a VIP «all projects» batch) so every new project
  can sit in the queue instead of being skipped at job 4. Execution is
  still ``parallel_jobs``. A captcha project is only queued for a user who
  uploaded their own solver key — same rule as the picker.
* **Once per project id.** The ``autoreg_done:<id>`` marker makes a reload
  storm harmless; it is set as soon as the sweep ran, whether or not anyone
  was eligible (an eligibility-based marker would re-run on every reload
  until somebody happened to qualify).
* **One personal DM when the run ends**, not a broadcast: each job finishes
  at its own time and reports its own counts to its own user.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bot import config
from bot import db as bot_db
from bot import limits as bot_limits
from bot import user_data as user_data_mod
from bot.http_client import log
from bot.registry import get_project

MARKER_PREFIX = "autoreg_done:"


def is_enabled() -> bool:
    """Master switch (AUTO_REG_ENABLED=0 kills the feature without losing
    anyone's opt-in flag)."""
    return str(getattr(config, "AUTO_REG_ENABLED", "1") or "1").strip() != "0"


def _marker(project_id: str) -> str:
    return f"{MARKER_PREFIX}{project_id}"


def run_for_new_project(project_id: str, start_job, *, force: bool = False) -> dict:
    """Queue one auto-reg job per eligible opted-in user.

    ``start_job`` is ``start_project_queue_job(user_id=…, chat_id=…,
    project=…)`` (injected so this module stays importable without tgbot).
    Returns a summary for the admin notice.

    ``force=True`` re-walks a project whose marker already fired (catch-up
    after a cap skip or a restart mid-sweep). Duplicate-project and empty
    prepare still refuse, so users who already have the job are not doubled.
    """
    pid = str(project_id or "").strip()
    if not pid or not is_enabled():
        return {"queued": 0, "skipped": 0, "reason": "disabled"}
    if bot_db.get_alert_marker(_marker(pid)) and not force:
        return {"queued": 0, "skipped": 0, "reason": "already_ran"}
    # Mark before running: a crash mid-sweep must not re-queue half the base
    # on the next reload — a missed user costs one manual tap, a double run
    # costs everybody's slots twice.
    bot_db.set_alert_marker(_marker(pid), datetime.now(timezone.utc).isoformat())

    project = get_project(pid, allow_runtime_disabled=True)
    if project is None:
        return {"queued": 0, "skipped": 0, "reason": "project_gone"}
    if bot_db.is_project_runtime_disabled(pid):
        return {"queued": 0, "skipped": 0, "reason": "disabled_runtime"}

    queued = skipped = 0
    for uid in bot_db.auto_reg_user_ids():
        try:
            if not user_data_mod.has_user_data(uid):
                skipped += 1
                continue
            if not bot_limits.project_allowed(project, uid):
                # Includes the captcha rule: no own key → no auto run on a
                # captcha project (the operator's balance is never a fallback).
                skipped += 1
                continue
            ok, _msg, job = start_job(
                user_id=uid, chat_id=uid, project=project
            )
            if ok and job is not None:
                job.auto_reg = True
                queued += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 — one user must not stop the sweep
            skipped += 1
            log(f"[autoreg] user={uid} project={pid}: {exc}")
    log(f"[autoreg] project={pid} queued={queued} skipped={skipped}")
    return {"queued": queued, "skipped": skipped, "reason": "ran"}


def finish_message(job, lang: str = "ru") -> str:
    """The personal DM a user gets when their auto-reg job ends."""
    from bot.i18n import t
    from bot.ux import esc

    name = str(getattr(job, "project_name", "") or job.project_id)
    ok_n = int(getattr(job, "ok", 0) or 0)
    already = int(getattr(job, "already", 0) or 0)
    done = int(getattr(job, "done", 0) or 0)
    total = int(getattr(job, "total", 0) or 0)
    failed = int(getattr(job, "fail", 0) or 0)
    bad = int(getattr(job, "bad", 0) or 0)
    # After a crash/lease resume the in-flight rows are re-submitted and the
    # wallet dedup answers ALREADY for rows already counted, so ok+already
    # (and done) can exceed the row count. Unique registrations never can.
    registered = min(ok_n + already, total) if total > 0 else ok_n + already
    done_n = done or (ok_n + already + failed + bad)
    if total > 0:
        done_n = min(done_n, total)
    return t(
        "autoreg.done",
        lang,
        project=esc(name),
        registered=registered,
        done=done_n,
        total=total,
    )
