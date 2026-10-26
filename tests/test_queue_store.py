"""Persistent queue store: leases and per-row payloads (bot/db/queue_jobs.py).

Pins the protocol properties the in-memory worker will come to rely on:

* create_job writes the job and all its rows in one transaction;
* claim is FIFO and installs a lease (owner + deadline + generation);
* renew / progress / finish / row writes are fenced by (owner, generation)
  — a worker whose lease was reclaimed can no longer speak for the job,
  not even for a single row of it;
* an expired lease is settled by the next claim: cancelled jobs finish as
  cancelled (never the immortal queued+cancel_requested), exhausted
  attempts die as lease_expired, everything else is requeued;
* cancel of a queued job is immediate and drops its rows; cancel of a
  running job is the cooperative flag between rows.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="queue_store_"))
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ["LIMITS_CACHE_TTL"] = "0"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import db as bot_db  # noqa: E402
from bot.db import queue_jobs as qj  # noqa: E402
from bot import submit_queue  # noqa: E402

bot_db.DB_PATH = str(_TMP / "t.db")  # type: ignore[attr-defined]
submit_queue.init_schema()
bot_db.init_db()


def _assert(cond: bool, msg: str = "") -> None:
    assert cond, msg


def _mk(job_user: int, project: str = "p1", n: int = 3, **kw) -> int:
    rows = [{"handle": f"h{i}", "wallet": f"0x{i:040x}"} for i in range(n)]
    return qj.create_job(
        user_id=job_user, project_id=project, chat_id=1, message_id=None,
        rows=rows, **kw
    )


def _drain() -> None:
    """Finish everything open — tests that assert on claim order start clean."""
    while True:
        got = qj.claim_next_job("drain")
        if got is None:
            return
        qj.finish_job(int(got["id"]), "drain", int(got["attempts"]), "done")


def test_create_job_is_atomic_with_rows() -> None:
    jid = _mk(101, n=3)
    job = qj.load_job(jid)
    _assert(job["status"] == "queued", job["status"])
    _assert(job["total"] == 3, job["total"])
    _assert(len(qj.job_rows(jid)) == 3, "rows stored")
    _assert(qj.job_rows(jid)[0][1]["handle"] == "h0", "seq order")


def test_claim_is_fifo_and_installs_lease() -> None:
    _drain()
    a = _mk(102, "pa")
    b = _mk(102, "pb")
    got = qj.claim_next_job("w1")
    _assert(got is not None and got["id"] == a, f"oldest first: {a},{b}")
    _assert(got["status"] == "running", "running")
    _assert(got["lease_owner"] == "w1", "lease owner")
    _assert(got["lease_until"], "lease deadline set")
    _assert(got["attempts"] == 1, "first generation")
    _assert(got["started_at"], "started_at set on first claim")


def test_empty_owner_is_rejected() -> None:
    _drain()
    _mk(112, "pz")
    try:
        qj.claim_next_job("")
    except ValueError:
        return
    _assert(False, "empty owner must raise")


def test_claim_respects_user_parallel() -> None:
    _drain()
    # 102 already has a running job from the previous test; cap of 1 blocks.
    _mk(102, "pc")
    got = qj.claim_next_job("w2", user_parallel_fn=lambda uid: 1)
    _assert(got is None or got["user_id"] != 102, "same user's second job waits")
    # ...but another user's job goes through.
    other = _mk(103, "pa")
    got2 = qj.claim_next_job("w2", user_parallel_fn=lambda uid: 1)
    _assert(got2 is not None and got2["id"] == other, "different user runs")
    qj.finish_job(other, "w2", int(got2["attempts"]), "done")


def test_claim_respects_project_lane() -> None:
    _drain()
    # pa already runs for user 102 (first claim test). Lane cap 1 → skip pa.
    _mk(104, "pa")
    got = qj.claim_next_job("w3", project_limit_fn=lambda pid: 1)
    _assert(got is None or got["project_id"] != "pa", "project lane respected")


def test_claim_respects_runtime_disable_gate() -> None:
    _drain()
    jid = _mk(113, "pblocked")
    got = qj.claim_next_job("w1", project_claimable_fn=lambda pid: pid != "pblocked")
    _assert(got is None or got["id"] != jid, "runtime-disabled project not claimed")
    _assert(qj.load_job(jid)["status"] == "queued", "stays queued for the cancel path")
    qj.request_cancel(jid)


def test_renew_and_progress_are_fenced() -> None:
    _drain()
    jid = _mk(105, "pd")
    job = qj.claim_next_job("w1")
    _assert(job is not None and job["id"] == jid, "claimed")
    gen = int(job["attempts"])
    _assert(qj.renew_lease(jid, "w1", gen), "owner renews")
    _assert(not qj.renew_lease(jid, "intruder", gen), "wrong owner cannot renew")
    _assert(not qj.renew_lease(jid, "w1", gen + 5), "stale generation cannot renew")
    _assert(qj.update_progress(jid, "w1", gen, done=2, ok=2), "owner writes progress")
    _assert(not qj.update_progress(jid, "intruder", gen, done=9), "stale owner cannot")
    _assert(not qj.update_progress(jid, "w1", gen + 5, done=9), "stale gen cannot")
    qj.finish_job(jid, "w1", gen, "done")


def test_finish_is_fenced_and_drops_rows() -> None:
    _drain()
    jid = _mk(106, "pe")
    job = qj.claim_next_job("w1")
    _assert(job is not None and job["id"] == jid, "claimed")
    gen = int(job["attempts"])
    _assert(not qj.finish_job(jid, "intruder", gen, "done"), "wrong owner refused")
    _assert(not qj.finish_job(jid, "w1", gen + 5, "done"), "stale gen refused")
    _assert(qj.finish_job(jid, "w1", gen, "done", counters={"ok": 3, "done": 3}),
            "owner finishes")
    after = qj.load_job(jid)
    _assert(after["status"] == "done", after["status"])
    _assert(after["lease_owner"] == "" and after["lease_until"] is None, "lease cleared")
    _assert(qj.job_rows(jid) == [], "rows dropped on finish")


def test_cancel_queued_is_immediate_and_drops_rows() -> None:
    _drain()
    jid = _mk(107, "pf")
    _assert(qj.request_cancel(jid), "cancel accepted")
    after = qj.load_job(jid)
    _assert(after["status"] == "cancelled", after["status"])
    _assert(qj.job_rows(jid) == [], "rows dropped")
    _assert(qj.claim_next_job("w1") is None or qj.claim_next_job("w1")["id"] != jid,
            "cancelled job never claimed")


def test_cancel_running_is_cooperative() -> None:
    _drain()
    jid = _mk(108, "pg")
    job = qj.claim_next_job("w1")
    _assert(job is not None and job["id"] == jid, "claimed")
    gen = int(job["attempts"])
    _assert(qj.request_cancel(jid), "cancel accepted")
    after = qj.load_job(jid)
    _assert(after["status"] == "running", "still running between rows")
    _assert(after["cancel_requested"] == 1, "flag set for the owner")
    _assert(qj.job_rows(jid), "rows kept until the owner finishes")
    qj.finish_job(jid, "w1", gen, "cancelled", detail="cancelled by user")


def test_expired_lease_is_swept_and_reclaimed() -> None:
    _drain()
    jid = _mk(109, "ph")
    job = qj.claim_next_job("w1")
    _assert(job is not None and job["id"] == jid, "claimed")
    with bot_db.db() as conn:
        conn.execute(
            "UPDATE submit_jobs SET lease_until=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", jid),
        )
        conn.commit()
    again = qj.claim_next_job("w2")
    _assert(again is not None and again["id"] == jid, "reclaimed by new owner")
    _assert(again["attempts"] == 2, "new generation")
    _assert(again["lease_owner"] == "w2", "new owner")
    # The old worker is mute now — on both fences.
    _assert(not qj.finish_job(jid, "w1", 1, "done"), "dead owner cannot finish")
    _assert(not qj.mark_row_done(jid, 0, "w1", 1), "dead owner cannot mark rows")
    _assert(qj.finish_job(jid, "w2", 2, "done"), "new owner can")


def test_cancelled_running_job_with_expired_lease_is_terminal() -> None:
    """cancel_requested + expired lease must never become the immortal
    queued-but-never-claimed job. Sweep finishes it as cancelled."""
    _drain()
    jid = _mk(114, "pk")
    job = qj.claim_next_job("w1")
    _assert(job is not None and job["id"] == jid, "claimed")
    _assert(qj.request_cancel(jid), "cancel requested while running")
    with bot_db.db() as conn:
        conn.execute(
            "UPDATE submit_jobs SET lease_until=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", jid),
        )
        conn.commit()
    nxt = qj.claim_next_job("w2")
    _assert(nxt is None or nxt["id"] != jid, "cancelled job is not reclaimed")
    after = qj.load_job(jid)
    _assert(after["status"] == "cancelled", f"swept to cancelled: {after['status']}")
    _assert(after["lease_owner"] == "", "lease cleared")
    _assert(qj.job_rows(jid) == [], "rows dropped")
    _assert(qj.open_jobs(114) == [], "not open anymore")


def test_lease_expiry_after_max_attempts_is_terminal() -> None:
    _drain()
    jid = _mk(110, "pi")
    with bot_db.db() as conn:
        conn.execute(
            "UPDATE submit_jobs SET status='running', lease_owner='ghost', "
            "attempts=?, lease_until=? WHERE id=?",
            (qj.DEFAULT_MAX_ATTEMPTS, "2000-01-01T00:00:00+00:00", jid),
        )
        conn.commit()
    got = qj.claim_next_job("w1")
    _assert(got is None or got["id"] != jid, "exhausted job is not re-claimed")
    after = qj.load_job(jid)
    _assert(after["status"] == "error", after["status"])
    _assert(after["detail"] == "lease_expired", after["detail"])
    _assert(qj.job_rows(jid) == [], "exhausted job drops its rows")


def test_row_lifecycle_is_fenced() -> None:
    _drain()
    jid = _mk(111, "pj")
    job = qj.claim_next_job("w1")
    gen = int(job["attempts"])
    _assert(qj.mark_row_running(jid, 0, "w1", gen), "owner starts row 0")
    _assert(not qj.mark_row_running(jid, 0, "w1", gen), "pending→running is once")
    _assert(qj.mark_row_done(jid, 0, "w1", gen), "owner finishes row 0")
    _assert(not qj.mark_row_done(jid, 1, "intruder", gen), "wrong owner refused")
    _assert(not qj.mark_row_done(jid, 1, "w1", gen + 5), "stale generation refused")
    # Steal the job: even the same owner name with a new generation fences
    # the old generation's writes (the fencing token, not the name, rules).
    with bot_db.db() as conn:
        conn.execute(
            "UPDATE submit_jobs SET lease_owner='w1', attempts=?, lease_until=? "
            "WHERE id=?",
            (gen + 1, "2999-01-01T00:00:00+00:00", jid),
        )
        conn.commit()
    _assert(not qj.mark_row_done(jid, 1, "w1", gen), "old generation is mute")
    _assert(qj.mark_row_running(jid, 1, "w1", gen + 1), "new generation works")
    # row 0 done (gone), row 1 running (retryable), row 2 pending.
    pending = qj.pending_rows(jid)
    _assert([seq for seq, _ in pending] == [1, 2], f"rows left: {pending}")
    qj.finish_job(jid, "w1", gen + 1, "done")


def test_row_running_visible_as_retryable() -> None:
    _drain()
    jid = _mk(115, "pl")
    job = qj.claim_next_job("w1")
    gen = int(job["attempts"])
    _assert(qj.mark_row_running(jid, 0, "w1", gen), "row 0 running")
    seqs = [seq for seq, _ in qj.pending_rows(jid)]
    _assert(0 in seqs, f"running counts as retryable after a reclaim: {seqs}")
    qj.finish_job(jid, "w1", gen, "done")


def test_crash_reclaim_row_lifecycle() -> None:
    """The full crash/reclaim arc for one row.

    w1 takes row 0 (running) and dies mid-HTTP; the lease expires; the
    sweep requeues the job and resets the row to pending; w2 claims the
    job with a new generation, can take the row again, and only w2 can
    record its outcome — the fencing silences w1's late HTTP answer.
    """
    _drain()
    jid = _mk(116, "pm")
    first = qj.claim_next_job("w1")
    _assert(first is not None and first["id"] == jid, "w1 claims")
    gen1 = int(first["attempts"])
    _assert(qj.mark_row_running(jid, 0, "w1", gen1), "w1 starts row 0")
    # w1 dies; its lease goes stale.
    with bot_db.db() as conn:
        conn.execute(
            "UPDATE submit_jobs SET lease_until=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", jid),
        )
        conn.commit()
    again = qj.claim_next_job("w2")
    _assert(again is not None and again["id"] == jid, "w2 reclaims")
    gen2 = int(again["attempts"])
    _assert(gen2 == gen1 + 1, "new generation")
    # The interrupted row is retryable AND startable by the new owner.
    seqs = [seq for seq, _ in qj.pending_rows(jid)]
    _assert(0 in seqs, f"row 0 listed as pending: {seqs}")
    _assert(qj.mark_row_running(jid, 0, "w2", gen2), "w2 re-takes row 0")
    # w1's late answer cannot land; w2's can.
    _assert(not qj.mark_row_done(jid, 0, "w1", gen1), "dead w1 is mute")
    _assert(qj.mark_row_done(jid, 0, "w2", gen2), "w2 records the row")
    _assert(qj.finish_job(jid, "w2", gen2, "done"), "w2 finishes the job")


def test_open_jobs_filter() -> None:
    _drain()
    jobs = qj.open_jobs(999999)
    _assert(jobs == [], "no jobs for unknown user")
    listed = qj.open_jobs()
    _assert(all(j["status"] in ("queued", "running") for j in listed), "open only")


def test_create_job_if_allowed_gates() -> None:
    _drain()
    ok = lambda uid: 3  # noqa: E731
    enabled = lambda pid: pid != "poff"  # noqa: E731
    jid, reason = qj.create_job_if_allowed(
        user_id=201, project_id="pq", chat_id=1, message_id=None,
        rows=[{"a": 1}], max_user_jobs_fn=ok, project_enabled_fn=enabled,
    )
    _assert(jid is not None and reason == "", f"first insert ok: {reason}")
    # Same user + same project while open → duplicate.
    j2, r2 = qj.create_job_if_allowed(
        user_id=201, project_id="pq", chat_id=1, message_id=None,
        rows=[{"a": 1}], max_user_jobs_fn=ok, project_enabled_fn=enabled,
    )
    _assert(j2 is None and r2 == "duplicate", f"duplicate refused: {r2}")
    # Runtime-disabled project → disabled, before any insert.
    j3, r3 = qj.create_job_if_allowed(
        user_id=202, project_id="poff", chat_id=1, message_id=None,
        rows=[{"a": 1}], max_user_jobs_fn=ok, project_enabled_fn=enabled,
    )
    _assert(j3 is None and r3 == "disabled", f"disabled refused: {r3}")
    # Per-user cap.
    one = lambda uid: 1  # noqa: E731
    qj.create_job_if_allowed(
        user_id=203, project_id="pr1", chat_id=1, message_id=None,
        rows=[{"a": 1}], max_user_jobs_fn=one, project_enabled_fn=enabled,
    )
    j4, r4 = qj.create_job_if_allowed(
        user_id=203, project_id="pr2", chat_id=1, message_id=None,
        rows=[{"a": 1}], max_user_jobs_fn=one, project_enabled_fn=enabled,
    )
    _assert(j4 is None and r4 == "full", f"cap refused: {r4}")


def test_unique_index_blocks_same_user_project_open_jobs() -> None:
    _drain()
    rows = [{"a": 1}]
    qj.create_job(user_id=204, project_id="ps", chat_id=1, message_id=None,
                  rows=rows)
    try:
        qj.create_job(user_id=204, project_id="ps", chat_id=1, message_id=None,
                      rows=rows)
    except Exception:
        return  # the index spoke
    # Index could not have been created (legacy duplicates) — not a failure
    # of this run, but the application gate must exist in that case.
    with bot_db.db() as conn:
        skipped = conn.execute(
            "SELECT value FROM bot_settings WHERE key='queue_duplicate_index_skipped'"
        ).fetchone()
    _assert(skipped is not None, "duplicate insert must be blocked one way or another")


def test_batch_gate_is_durable() -> None:
    _drain()
    _assert(qj.begin_user_batch(301, 77), "first begin claims the gate")
    _assert(not qj.begin_user_batch(301, 78), "second begin refused")
    _assert(qj.user_batch_gate(301) == 77, "gate holds the batch id")
    _assert(qj.begin_user_batch(302, 1), "other user unaffected")
    qj.end_user_batch(301)
    _assert(qj.user_batch_gate(301) == 0, "gate released")
    _assert(qj.begin_user_batch(301, 79), "begin works after release")
    # Stale gate: a crash between begin and end must not lock the user out.
    with bot_db.db() as conn:
        conn.execute(
            "UPDATE user_batch_gates SET created_at=? WHERE user_id=?",
            ("2000-01-01T00:00:00+00:00", 301),
        )
        conn.commit()
    _assert(qj.begin_user_batch(301, 80), "stale gate is stolen, not obeyed")
    qj.end_user_batch(301)
    qj.end_user_batch(302)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for tfn in tests:
        try:
            tfn()
            print(f"PASS {tfn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {tfn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        raise SystemExit(1)
    print(f"ALL {len(tests)} PASSED")


if __name__ == "__main__":
    main()
