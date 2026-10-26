"""The worker-loop error path must release everything the done path releases.

`_process_job` raising used to leave the job claimable-forever, its rows in
submit_job_rows, and the project's artifact undelivered (rows settled before
the crash are real — the user paid slots for them). Under the lease protocol
the error path must additionally finish the job through the fence: the DB
row has to go terminal, or the expired lease would resurrect a job whose
worker just crashed for a non-transient reason.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="worker_err_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "t:t"
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["JOB_PAYLOAD_DIR"] = str(_TMP / "payloads")
os.environ["LIMITS_CACHE_TTL"] = "0"

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot.db import queue_jobs as qj  # noqa: E402
from bot import submit_queue as sq  # noqa: E402

config.DB_PATH = _TMP / "t.db"
bot_db.DB_PATH = _TMP / "t.db"  # type: ignore[attr-defined]
config.JOB_PAYLOAD_DIR = _TMP / "payloads"


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class StubProject:
    """Records deliver/cleanup so the test can see the hooks fire."""

    def __init__(self) -> None:
        self.id = "stuberr"
        self.name = "Stub"
        self.ok_limit = 1000
        self.queue_parallel = 1
        self.max_job_parallel = None
        self.self_sourced = False
        self.bulk_columns = [("handle", True, "h"), ("wallet", True, "w")]
        self.queue_prepare = None
        self.queue_before_submit = None
        self.queue_after_submit = None
        self.queue_cleanup = None
        self.project_concurrency = None
        self.auto_tweet_urls = 0
        self.delivered: list[int] = []

    def queue_deliver(self, *, job_id, **_kw) -> None:  # noqa: ANN001
        self.delivered.append(job_id)


class FakeBot:
    def send_message(self, *_a, **_k):
        class M:
            message_id = 1

        return M()

    def edit_message_text(self, *_a, **_k):
        return None


def test_error_path_finishes_job_delivers_and_purges() -> None:
    bot_db.init_db()
    sq.init_schema()
    uid = 812301
    bot_db.ensure_user(uid, "u", "U")

    project = StubProject()
    rows = [{"handle": f"h{i}", "wallet": "0x" + f"{i:040x}"} for i in range(3)]
    job = sq.enqueue(
        user_id=uid, project=project, chat_id=uid, message_id=None, rows=rows
    )
    _ok(qj.job_rows(job.id), "enqueue must have stored the rows")

    saved = {
        "process": sq._process_job,
        "bot": sq._bot,
        "get_project": sq.get_project,
        "purge": sq._purge_job_soon,
    }
    purged: list[int] = []

    def boom(_job):
        raise RuntimeError("boom: simulate a crash mid-job")

    status = ""
    detail = ""
    try:
        sq._process_job = boom
        sq._bot = FakeBot()
        sq.get_project = lambda pid, **_kw: project
        sq._purge_job_soon = lambda jid, **_kw: purged.append(int(jid))
        sq._stop.clear()

        worker = threading.Thread(target=sq._worker_loop, args=(99,), daemon=True)
        worker.start()
        deadline = time.time() + 10
        row = None
        while time.time() < deadline:
            row = qj.load_job(job.id)
            status = str(row["status"]) if row else ""
            detail = str(row["detail"] or "") if row else ""
            if status == "error":
                break
            time.sleep(0.02)
        sq._stop.set()
        worker.join(timeout=5)
    finally:
        sq._process_job = saved["process"]
        sq._bot = saved["bot"]
        sq.get_project = saved["get_project"]
        sq._purge_job_soon = saved["purge"]
        sq._stop.clear()
        sq.reset_runtime_state()

    _ok(status == "error", f"DB status={status}")
    _ok("boom" in detail, f"detail lost the cause: {detail}")
    _ok(qj.job_rows(job.id) == [], "rows survived the error path")
    _ok(project.delivered == [job.id],
        f"artifact not delivered on error: {project.delivered}")
    _ok(purged == [job.id],
        f"job not scheduled for purge (stays in _jobs forever): {purged}")
    claimed_again = qj.claim_next_job("postmortem", project_claimable_fn=lambda p: True)
    _ok(claimed_again is None or claimed_again["id"] != job.id,
        "a crashed-out job must not be claimable again")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
