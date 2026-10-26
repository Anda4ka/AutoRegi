"""BOOT_RESUME_JOBS: interrupted jobs re-enqueue after a restart.

The default remains the old contract — mark ``bot_restart``, tell the user to
start again — because replaying a row the endpoint may have already accepted
is a decision, not a default. With the flag on, the persisted payload goes
back through ``enqueue_if_allowed`` (runtime-disable, duplicate, cap re-checked)
and the global wallet dedup keeps already-landed rows off the wire.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="boot_resume_"))
_DB = _TMP / "test.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["JOB_PAYLOAD_DIR"] = str(_TMP / "job_payloads")
os.environ["BOT_RESULTS"] = str(_TMP / "results.jsonl")

for _mod in list(sys.modules):
    if (
        _mod == "bot"
        or _mod.startswith("bot.")
        or _mod == "tgbot"
        or _mod == "handlers"
        or _mod.startswith("handlers.")
    ):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot.db import queue_jobs as qj  # noqa: E402
from bot import submit_queue as sq  # noqa: E402

bot_db.DB_PATH = str(_DB)  # type: ignore[attr-defined]
config.JOB_PAYLOAD_DIR = _TMP / "job_payloads"
bot_db.init_db()
sq.init_schema()

UID = 900111
WALLET = "0x" + "bb" * 20
ROWS = [{"handle": "resumer", "wallet": WALLET}]


def _seed_interrupted(project_id: str) -> int:
    bot_db.ensure_user(UID, "resumer", "R")
    now = datetime.now(timezone.utc).isoformat()
    with bot_db.db() as conn:
        cur = conn.execute(
            """
            INSERT INTO submit_jobs
                (user_id, project_id, status, total, done, chat_id, created_at)
            VALUES (?, ?, 'running', 1, 0, 123, ?)
            """,
            (UID, project_id, now),
        )
        job_id = int(cur.lastrowid)
        conn.commit()
    payload = config.JOB_PAYLOAD_DIR / f"{job_id}.json"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(
        '[{"handle":"resumer","wallet":"' + WALLET + '"}]', encoding="utf-8"
    )
    return job_id


def _active_jobs():
    return sq.user_active_jobs(UID)


def _reset_queue_state() -> None:
    """One queue per test: jobs from a previous test would trip the
    duplicate gate and make the refusal test fail for the wrong reason."""
    with sq._lock:
        sq._jobs.clear()

    with bot_db.db() as conn:
        conn.execute("DELETE FROM submit_job_rows")
        conn.execute("DELETE FROM submit_jobs")
        conn.commit()


def test_resume_off_keeps_old_contract() -> None:
    _reset_queue_state()
    config.BOOT_RESUME_JOBS = False
    job_id = _seed_interrupted("example")
    handled = sq.reconcile_zombie_jobs_on_boot()
    assert handled >= 1
    with bot_db.db() as conn:
        row = conn.execute(
            "SELECT status, detail FROM submit_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row["status"] == "error" and row["detail"] == "bot_restart"
    assert not (config.JOB_PAYLOAD_DIR / f"{job_id}.json").exists()
    assert _active_jobs() == []


def test_resume_on_reenqueues_through_gates() -> None:
    _reset_queue_state()
    config.BOOT_RESUME_JOBS = True
    job_id = _seed_interrupted("example")
    sq.reconcile_zombie_jobs_on_boot()
    active = _active_jobs()
    assert len(active) == 1
    resumed = active[0]
    assert resumed.project_id == "example"
    assert resumed.id != job_id
    # the resumed job's rows live in submit_job_rows now, not a payload file
    assert [r for _seq, r in qj.job_rows(resumed.id)] == ROWS
    # old row errored, its legacy payload consumed
    with bot_db.db() as conn:
        row = conn.execute(
            "SELECT status, detail FROM submit_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row["status"] == "error" and row["detail"] == "bot_restart"
    assert not (config.JOB_PAYLOAD_DIR / f"{job_id}.json").exists()
    # and it is actually claimable by a worker, not just in the DB
    claimed = qj.claim_next_job(
        "resume-probe", project_claimable_fn=lambda pid: pid == "example"
    )
    assert claimed is not None and claimed["id"] == resumed.id
    qj.finish_job(resumed.id, "resume-probe", int(claimed["attempts"]), "cancelled")


def test_resume_refuses_unknown_project() -> None:
    _reset_queue_state()
    config.BOOT_RESUME_JOBS = True
    job_id = _seed_interrupted("no_such_project_xyz")
    sq.reconcile_zombie_jobs_on_boot()
    with bot_db.db() as conn:
        row = conn.execute(
            "SELECT status, detail FROM submit_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row["status"] == "error" and row["detail"] == "bot_restart"
    assert _active_jobs() == []


def test_second_reconcile_does_not_double_resume() -> None:
    _reset_queue_state()
    config.BOOT_RESUME_JOBS = True
    _seed_interrupted("example")
    sq.reconcile_zombie_jobs_on_boot()
    first = _active_jobs()
    assert len(first) == 1
    # A second boot mid-queue must not stack a second job for the project:
    # the interrupted row is consumed, the duplicate gate refuses the rest.
    sq.reconcile_zombie_jobs_on_boot()
    again = _active_jobs()
    assert len(again) == 1
    assert again[0].project_id == "example"
