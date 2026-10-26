"""End-to-end smoke test of `_process_job` — the queue's hot path.

Written after a live incident: refactoring the thread-count calculation into
`resolve_job_parallel` left stale `n_par` references further down
`_process_job`, so every job died instantly with

    NameError: name 'n_par' is not defined   (jobs #1578, #1581, 2026-08-14)

Every existing test poked at helpers; none ran a job through the worker, so a
broken hot path shipped green. This one runs real rows through `_process_job`
with a stubbed submit function and a stubbed project, on both branches
(single-threaded and parallel) — the two places the bug lived.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="jobsmoke_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "t:t"
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["JOB_PAYLOAD_DIR"] = str(_TMP / "payloads")
os.environ["LIMITS_CACHE_TTL"] = "0"
os.environ["BASE_JOB_PARALLEL"] = "5"
os.environ["VIP_JOB_PARALLEL"] = "20"

# Purge `tgbot` alongside `bot.*`: leaving it cached would keep it bound to the
# bot modules this file just dropped, and the next module to `import tgbot`
# would silently get one wired to another test's database.
for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import limits as bot_limits  # noqa: E402
from bot import submit_queue as sq  # noqa: E402

config.DB_PATH = _TMP / "t.db"
bot_db.DB_PATH = _TMP / "t.db"  # type: ignore[attr-defined]
config.JOB_PAYLOAD_DIR = _TMP / "payloads"


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class StubProject:
    """Minimal stand-in for a registry Project."""

    def __init__(self, pid="stub", *, queue_parallel=None, max_job_parallel=None):
        self.id = pid
        self.name = "Stub Project"
        self.ok_limit = 1000
        self.queue_parallel = queue_parallel
        self.max_job_parallel = max_job_parallel
        self.self_sourced = False
        self.bulk_columns = [("handle", True, "h"), ("wallet", True, "w")]
        self.queue_prepare = None
        self.queue_before_submit = None
        self.queue_after_submit = None
        self.queue_cleanup = None
        self.project_concurrency = None
        self.auto_tweet_urls = 0

    def parse(self, line):  # pragma: no cover - not used by _process_job
        parts = line.split("|")
        return {"handle": parts[0], "wallet": parts[1]} if len(parts) > 1 else None

    def validate(self, fields):  # pragma: no cover
        return None


class FakeBot:
    def send_message(self, *_a, **_k):
        class M:
            message_id = 1

        return M()

    def edit_message_text(self, *_a, **_k):
        return None


def _run(project: StubProject, uid: int, n_rows: int) -> sq.QueueJob:
    """Enqueue n_rows for uid and run the job synchronously."""
    bot_db.init_db()
    sq.init_schema()
    bot_db.ensure_user(uid, "u", "U")
    bot_limits.invalidate(uid)

    rows = [{"handle": f"h{i}", "wallet": "0x" + f"{i:040x}"} for i in range(n_rows)]
    job = sq.enqueue(
        user_id=uid, project=project, chat_id=uid, message_id=None, rows=rows
    )

    seen: list[dict] = []

    def fake_submit(_project, fields, _user_id):
        seen.append(fields)
        return {"status": "OK", "detail": "stub", "handle": fields.get("handle")}

    real_get, real_fn, real_bot = sq.get_project, sq._submit_fn, sq._bot
    sq.get_project = lambda pid, **_kw: project
    sq._submit_fn = fake_submit
    sq._bot = FakeBot()
    try:
        sq._process_job(job)
    finally:
        sq.get_project, sq._submit_fn, sq._bot = real_get, real_fn, real_bot
    job.seen = seen  # type: ignore[attr-defined]
    return job


def test_single_threaded_branch_runs() -> None:
    """queue_parallel=1 → the `if job.parallel <= 1` path."""
    job = _run(StubProject("stub1", queue_parallel=1), 80101, 4)
    _ok(job.status == "done", f"status={job.status} detail={job.detail}")
    _ok(job.parallel == 1, f"parallel={job.parallel}")
    _ok(job.ok == 4, f"ok={job.ok} of {job.total}")
    _ok(job.done == 4, f"done={job.done}")
    _ok(len(job.seen) == 4, "every row must reach the submit function")


def test_parallel_branch_runs() -> None:
    """Tier parallelism → the ThreadPoolExecutor path."""
    job = _run(StubProject("stub2"), 80102, 12)
    _ok(job.status == "done", f"status={job.status} detail={job.detail}")
    _ok(job.parallel == int(config.BASE_JOB_PARALLEL), f"parallel={job.parallel}")
    _ok(job.ok == 12, f"ok={job.ok} of {job.total}")
    _ok(len(job.seen) == 12, "every row must reach the submit function")


def test_project_ceiling_applies_to_a_real_run() -> None:
    job = _run(StubProject("stub3", max_job_parallel=2), 80103, 6)
    _ok(job.status == "done", f"status={job.status} detail={job.detail}")
    _ok(job.parallel == 2, f"ceiling ignored: parallel={job.parallel}")
    _ok(job.ok == 6, f"ok={job.ok}")


def test_job_never_dies_with_a_python_error() -> None:
    """A NameError/AttributeError in the hot path shows up as `error` + detail."""
    for project, uid, n in (
        (StubProject("stub4", queue_parallel=1), 80104, 3),
        (StubProject("stub5"), 80105, 8),
    ):
        job = _run(project, uid, n)
        detail = (job.detail or "").lower()
        _ok(job.status != "error", f"job errored: {job.detail}")
        for marker in ("not defined", "nameerror", "attributeerror", "traceback"):
            _ok(marker not in detail, f"python-level failure leaked: {job.detail}")


def main() -> None:
    tests = [
        test_single_threaded_branch_runs,
        test_parallel_branch_runs,
        test_project_ceiling_applies_to_a_real_run,
        test_job_never_dies_with_a_python_error,
    ]
    failed = 0
    for tfn in tests:
        try:
            tfn()
            print(f"PASS {tfn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {tfn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
