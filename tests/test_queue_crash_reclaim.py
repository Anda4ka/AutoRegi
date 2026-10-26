"""Crash/reclaim integration: a worker dies mid-job, another resumes it.

Phase 2 acceptance test. Runs the real claim → _process_job path with a
submit function that blocks on demand, kills the first worker mid-job,
expires its lease, and lets a second worker reclaim through the normal
claim scan. What must hold:

* the interrupted row is submitted again (it was in flight, not done) and
  no finished row is submitted twice;
* the dead worker's late writes are fenced out;
* the job finishes `done` under the new worker with correct counters.
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

_TMP = Path(tempfile.mkdtemp(prefix="crash_reclaim_"))
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
from bot import submit_queue as sq  # noqa: E402

qj = sq.qj

config.DB_PATH = _TMP / "t.db"
bot_db.DB_PATH = _TMP / "t.db"  # type: ignore[attr-defined]
config.JOB_PAYLOAD_DIR = _TMP / "payloads"


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class StubProject:
    def __init__(self) -> None:
        self.id = "crashy"
        self.name = "Crashy"
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


class FakeBot:
    def send_message(self, *_a, **_k):
        class M:
            message_id = 1

        return M()

    def edit_message_text(self, *_a, **_k):
        return None


def test_worker_crash_mid_row_is_resumed_by_next_worker() -> None:
    bot_db.init_db()
    sq.init_schema()
    sq.reset_runtime_state()
    uid = 91001
    bot_db.ensure_user(uid, "u", "U")

    project = StubProject()
    rows = [
        {"handle": "h0", "wallet": "0x" + "00" * 20},
        {"handle": "h1", "wallet": "0x" + "11" * 20},
        {"handle": "h2", "wallet": "0x" + "22" * 20},
    ]
    job = sq.enqueue(
        user_id=uid, project=project, chat_id=uid, message_id=None, rows=rows
    )

    submitted: list[str] = []
    finish_first = threading.Event()
    first_started = threading.Event()
    submit_lock = threading.Lock()
    first_owner: dict = {}

    def _submit(_project, fields, _user_id):
        with submit_lock:
            submitted.append(fields["handle"])
        if fields["handle"] == "h1" and not first_started.is_set():
            first_started.set()
            # The "crash": this worker never answers row h1 and never
            # finishes the job — like a kill -9 mid-HTTP. Only the first
            # h1 blocks; the reclaim's second h1 answers normally.
            finish_first.wait(timeout=10)
        return {"status": "OK", "detail": "stub"}

    saved = {"fn": sq._submit_fn, "bot": sq._bot, "gp": sq.get_project}
    sq._submit_fn = _submit
    sq._bot = FakeBot()
    sq.get_project = lambda pid, **_kw: project
    sq._stop.clear()
    try:
        worker1 = threading.Thread(target=sq._worker_loop, args=(1,), daemon=True)
        worker1.start()
        _ok(first_started.wait(timeout=10), "worker1 reached the blocking row")
        # h0 submitted and recorded; h1 in flight, unrecorded.
        with bot_db.db() as conn:
            r = conn.execute(
                "SELECT lease_owner, attempts FROM submit_jobs WHERE id=?",
                (job.id,),
            ).fetchone()
        first_owner.update(owner=str(r["lease_owner"]), gen=int(r["attempts"]))
        with bot_db.db() as conn:
            row_states = {
                int(rr["seq"]): str(rr["status"])
                for rr in conn.execute(
                    "SELECT seq, status FROM submit_job_rows WHERE job_id=?",
                    (job.id,),
                )
            }
        _ok(row_states.get(0) == "done", f"row 0 recorded: {row_states}")
        _ok(row_states.get(1) == "running", f"row 1 in flight: {row_states}")

        # Kill worker1's job without any cleanup: expire its lease from
        # outside, as time would.
        with bot_db.db() as conn:
            conn.execute(
                "UPDATE submit_jobs SET lease_until=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", job.id),
            )
            conn.commit()

        worker2 = threading.Thread(target=sq._worker_loop, args=(2,), daemon=True)
        worker2.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            row = qj.load_job(job.id)
            if row["status"] == "done":
                break
            time.sleep(0.05)
        # Release the "crashed" HTTP call; its late answer must land nowhere.
        finish_first.set()
        worker1.join(timeout=5)
        worker2.join(timeout=5)
        sq._stop.set()

        row = qj.load_job(job.id)
        _ok(row["status"] == "done", f"job finished: {row['status']} {row['detail']}")
        _ok(int(row["ok"]) == 3, f"three OKs: {row['ok']}")
        _ok(int(row["done"]) == 3, f"three done: {row['done']}")
        # h1 was in flight at the crash → resubmitted by the new worker.
        _ok(submitted.count("h1") == 2, f"h1 resubmitted once: {submitted}")
        # rows settled and dropped at finish
        _ok(qj.job_rows(job.id) == [], "rows dropped at finish")
        # the dead worker could not finish or record after the reclaim
        _ok(not qj.finish_job(job.id, first_owner["owner"], first_owner["gen"],
                              "done"), "dead worker cannot finish")
    finally:
        finish_first.set()
        sq._stop.set()
        sq._submit_fn = saved["fn"]
        sq._bot = saved["bot"]
        sq.get_project = saved["gp"]
        sq.reset_runtime_state()


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
