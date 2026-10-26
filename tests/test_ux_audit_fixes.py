"""Tests for the post-audit fixes (P1/P2) on top of the UX overhaul.

Covers:
  * P1.1 atomic enqueue_if_allowed: duplicate + cap re-checked under _lock
    (double-start cannot create two jobs for the same project).
  * P2.3 zero slots → no start button + «limit reached» text;
    will_send capped by remaining slots.
  * P2.4 qcancel:no:<id> targets the exact job.
  * P2.6 runtime-disabled project is rejected at enqueue_if_allowed.
  * P2.8 no proxies → no start button + «Proxies: none» on the screen.
  * P2.7 channel gate strings exist in EN (subscribe_text localized).
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="ux_fixes_test_"))
_DB = _TMP / "test.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
# Redirect per-user storage to the temp dir so the tests NEVER touch the real
# production data/user_data or data/user_proxies directories.
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["JOB_PAYLOAD_DIR"] = str(_TMP / "job_payloads")
os.environ["BOT_RESULTS"] = str(_TMP / "results.jsonl")

# `tgbot` is purged with `bot.*` on purpose. Left cached, it keeps the bot
# modules dropped here — so this file's writes land in one database while
# tgbot reads another, and the mismatch only shows up in whole-suite runs.
for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import project_health  # noqa: E402
from bot import submit_queue as sq  # noqa: E402
from bot import user_data as ud  # noqa: E402
from bot.channel import subscribe_keyboard, subscribe_text  # noqa: E402

config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]

import tgbot  # noqa: E402
from bot import proxy_pool  # noqa: E402

from bot.registry import get_project, list_projects, load_projects  # noqa: E402

load_projects()
proxy_pool._proxies_loaded = True  # prevent lazy reload from a real proxy.txt


def _set_proxies(pool) -> None:
    """Set the shared proxy pool for ONE test (order-independent, pytest-safe)."""
    proxy_pool._proxies[:] = list(pool)


def _assert(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _seed(uid: int, n: int = 0) -> None:
    bot_db.ensure_user(uid, "u", "U")
    if n > 0:
        data = "\n".join(
            f"u{i}|0x" + ("%02x" % (i + 1)) * 20 for i in range(n)
        )
        ud.save_user_data_from_bytes(uid, data.encode("utf-8"))
    else:
        ud.clear_user_data(uid)


def _btn_texts(kb) -> list[str]:
    return [b.text for row in kb.keyboard for b in row]


def _has_cb(kb, prefix: str) -> bool:
    return any(
        (b.callback_data or "").startswith(prefix)
        for row in kb.keyboard
        for b in row
    )


def _make_rows(uid: int, project, n: int) -> list[dict]:
    ud.save_user_data_from_bytes(
        uid,
        ("\n".join(f"u{i}|0x" + ("%02x" % (i + 1)) * 20 for i in range(n))).encode(),
    )
    rows, _errs, _skip = sq.prepare_job_rows(uid, project)
    return rows


def test_atomic_enqueue_rejects_duplicate() -> None:
    """P1.1: a second enqueue_if_allowed for the same user+project is rejected."""
    bot_db.init_db()
    sq.init_schema()
    # reset in-memory queue state
    sq._jobs.clear()

    uid = 20001
    _seed(uid, n=3)
    proj = get_project("example")
    rows = _make_rows(uid, proj, 3)
    job1, reason1, existing1 = sq.enqueue_if_allowed(
        user_id=uid, project=proj, chat_id=1, message_id=None, rows=rows
    )
    _assert(job1 is not None and reason1 == "", f"first should succeed: {reason1}")
    # Second call (same user+project) must be rejected as duplicate, atomically.
    job2, reason2, existing2 = sq.enqueue_if_allowed(
        user_id=uid, project=proj, chat_id=1, message_id=None, rows=rows
    )
    _assert(job2 is None, "second enqueue must not create a job")
    _assert(reason2 == "duplicate", f"reason={reason2}")
    _assert(existing2 is not None and existing2.id == job1.id)
    # cleanup
    sq.cancel_user_job(uid, job_id=job1.id)


def test_atomic_enqueue_concurrent_double_start() -> None:
    """P1.1: two threads starting the same project → exactly one job."""
    sq._jobs.clear()

    uid = 20002
    _seed(uid, n=4)
    proj = get_project("example")
    rows = _make_rows(uid, proj, 4)
    results: list = []
    lock = threading.Lock()

    def _try():
        job, reason, _ = sq.enqueue_if_allowed(
            user_id=uid, project=proj, chat_id=1, message_id=None, rows=rows
        )
        with lock:
            results.append((job, reason))

    t1 = threading.Thread(target=_try)
    t2 = threading.Thread(target=_try)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    created = [r[0] for r in results if r[0] is not None]
    _assert(len(created) == 1, f"expected exactly 1 job, got {len(created)}")
    _assert(any(r[1] == "duplicate" for r in results if r[0] is None), results)
    sq.cancel_user_job(uid, job_id=created[0].id)


def test_atomic_enqueue_cap_breach() -> None:
    """P1.1: exceeding USER_MAX_ACTIVE_JOBS via parallel enqueue is blocked."""
    sq._jobs.clear()

    uid = 20003
    nmax = int(getattr(config, "USER_MAX_ACTIVE_JOBS", 3) or 3)
    _seed(uid, n=2)
    # Take whatever is live rather than naming projects: the catalogue changes,
    # and an admin runtime-disable hides one from get_project at any moment.
    projects = [p for p in list_projects() if not getattr(p, "self_sourced", False)]
    if len(projects) < nmax + 1:
        import pytest
        pytest.skip("cap-breach test needs more live projects than USER_MAX_ACTIVE_JOBS")
    projects = projects[: nmax + 1]
    created = []
    for p in projects:
        rows = _make_rows(uid, p, 2)
        if not rows:
            continue
        job, reason, _ = sq.enqueue_if_allowed(
            user_id=uid, project=p, chat_id=1, message_id=None, rows=rows
        )
        if job is not None:
            created.append(job)
        elif reason != "full":
            # duplicate or disabled acceptable-ish; full is what we test next
            pass
        if len(created) >= nmax:
            break
    _assert(len(created) <= nmax, f"cap breached: {len(created)} > {nmax}")
    # One more must be rejected as full.
    extra = next(p for p in projects if p.id not in {j.project_id for j in created})
    rows = _make_rows(uid, extra, 2)
    job, reason, _ = sq.enqueue_if_allowed(
        user_id=uid, project=extra, chat_id=1, message_id=None, rows=rows
    )
    _assert(job is None and reason == "full", f"expected full, got {reason}")
    # Auto-reg lifts the cap so every project can sit in the queue.
    job_ar, reason_ar, _ = sq.enqueue_if_allowed(
        user_id=uid, project=extra, chat_id=1, message_id=None, rows=rows,
        auto_reg=True,
    )
    _assert(job_ar is not None and reason_ar == "", f"auto_reg should enqueue: {reason_ar}")
    _assert(getattr(job_ar, "auto_reg", False), "auto_reg bit persisted on the job")
    created.append(job_ar)
    for j in created:
        sq.cancel_user_job(uid, job_id=j.id)


def test_runtime_disabled_rejected_at_enqueue() -> None:
    """P2.6: a project runtime-disabled by admin is rejected atomically."""
    sq._jobs.clear()

    uid = 20004
    _seed(uid, n=2)
    proj = get_project("example")
    bot_db.set_project_runtime_disabled(proj.id, True, admin_id=1)
    try:
        rows = _make_rows(uid, proj, 2)
        job, reason, _ = sq.enqueue_if_allowed(
            user_id=uid, project=proj, chat_id=1, message_id=None, rows=rows
        )
        _assert(job is None and reason == "disabled", f"reason={reason}")
    finally:
        bot_db.set_project_runtime_disabled(proj.id, False, admin_id=1)


def test_zero_slots_hides_start() -> None:
    """P2.3: limit reached → no start button + «limit reached» text."""
    uid = 20005
    _seed(uid, n=2)
    proj = get_project("example")
    lim = 10  # free default
    for i in range(lim):
        bot_db.record_submission(uid, proj.id, "h%d" % i, "0x" + ("%02x" % (i + 50)) * 40, "OK", "")
    txt = tgbot.project_ready_text(proj, bot_db.get_ok_count(uid, proj.id), uid, lang="ru")
    _assert("Лимит регистраций выполнен" in txt, txt)
    kb = tgbot.project_screen_keyboard(proj, lang="ru", user_id=uid)
    _assert(not _has_cb(kb, "queue:start:"), "start button must be hidden at 0 slots")


def test_will_send_capped_by_slots() -> None:
    """P2.3: with more accounts than slots, the screen shows the slot-capped count."""
    uid = 20006
    _seed(uid, n=24)
    proj = get_project("example")
    # Give shared proxies so can_start is true and the start path is exercised.
    _set_proxies(["http://u:p@1.2.3.4:8080"])
    try:
        ok = bot_db.get_ok_count(uid, proj.id)  # 0
        left = 10 - ok
        txt = tgbot.project_ready_text(proj, ok, uid, lang="ru")
        # Russian string: «Будет отправлено: <b>{n}</b> аккаунт(ов) из 24 (лимит слотов)»
        _assert("из 24" in txt and str(left) in txt, txt)
        kb = tgbot.project_screen_keyboard(proj, lang="ru", user_id=uid)
        # Start button shows the capped number, not 24.
        start_btn = next(
            (b for row in kb.keyboard for b in row if (b.callback_data or "").startswith("queue:start:")),
            None,
        )
        _assert(start_btn is not None and "24" not in start_btn.text, start_btn)
        _assert(str(left) in start_btn.text, start_btn.text)
    finally:
        _set_proxies([])


def test_no_proxies_hides_start_with_warning() -> None:
    """P2.8: no own/shared proxies + project not allow_direct → no start, warning shown."""
    uid = 20007
    _seed(uid, n=5)
    proj = get_project("example")  # allow_direct False
    _set_proxies([])  # no shared
    ud.clear_user_data(uid)
    ud.save_user_data_from_bytes(
        uid, ("\n".join(f"u{i}|0x" + ("%02x" % (i + 1)) * 20 for i in range(5))).encode()
    )
    txt = tgbot.project_ready_text(proj, 0, uid, lang="ru")
    _assert("Прокси: ❌ нет" in txt, txt)
    kb = tgbot.project_screen_keyboard(proj, lang="ru", user_id=uid)
    _assert(not _has_cb(kb, "queue:start:"), "start must be hidden without proxies")


def test_health_config_hides_start() -> None:
    """Missing mandatory external config must hide the start action."""
    uid = 20011
    _seed(uid, n=3)
    proj = get_project("example")
    _set_proxies(["http://u:p@1.2.3.4:8080"])
    project_health.save_result(
        project_health.HealthResult(
            project_id=proj.id,
            status="config",
            detail="captcha key missing",
            checked_at="2026-08-11T00:00:00+00:00",
        )
    )
    try:
        txt = tgbot.project_ready_text(proj, 0, uid, lang="ru")
        kb = tgbot.project_screen_keyboard(proj, lang="ru", user_id=uid)
        _assert("требуется настройка" in txt, txt)
        _assert(not _has_cb(kb, "queue:start:"), "config-blocked start hidden")
    finally:
        bot_db.set_setting(f"project_health:{proj.id}", "")
        _set_proxies([])


def test_queue_start_markup_hidden_without_proxies() -> None:
    """#2: /queue must NOT offer Start when proxies are unavailable (same gate
    as the project screen — no bypass via /queue)."""
    uid = 20008
    _seed(uid, n=5)
    proj = get_project("example")
    bot_db.set_selected_project(uid, proj.id)
    _set_proxies([])  # no shared pool; no own proxies for this user
    try:
        kb = tgbot._queue_start_markup(uid, "ru")
        if kb is not None:
            _assert(
                not _has_cb(kb, "queue:start:"),
                "Start must not appear in /queue without proxies",
            )
        # With proxies available, Start reappears.
        _set_proxies(["http://u:p@1.2.3.4:8080"])
        kb2 = tgbot._queue_start_markup(uid, "ru")
        _assert(kb2 is not None and _has_cb(kb2, "queue:start:"), "Start should appear with proxies")
    finally:
        _set_proxies([])


def test_queue_start_markup_hidden_at_zero_slots() -> None:
    """#2: /queue must NOT offer Start when slots are exhausted."""
    uid = 20009
    _seed(uid, n=3)
    proj = get_project("example")
    bot_db.set_selected_project(uid, proj.id)
    _set_proxies(["http://u:p@1.2.3.4:8080"])
    try:
        lim = 10
        for i in range(lim):
            bot_db.record_submission(uid, proj.id, "h%d" % i, "0x" + ("%02x" % (i + 70)) * 40, "OK", "")
        kb = tgbot._queue_start_markup(uid, "ru")
        if kb is not None:
            _assert(not _has_cb(kb, "queue:start:"), "Start must not appear at 0 slots")
    finally:
        _set_proxies([])


def test_gate_strings_localized_en() -> None:
    """P2.7: subscribe screen + keyboard render in EN."""
    en_text = subscribe_text(lang="en")
    _assert("subscribers" in en_text.lower() or "subscribe" in en_text.lower(), en_text)
    kb = subscribe_keyboard(lang="en")
    labels = [b.text for row in kb.keyboard for b in row]
    _assert(any("Subscribe" in x for x in labels), labels)
    assert any("Check" in x for x in labels)
    # RU still works
    ru_text = subscribe_text(lang="ru")
    _assert("подписчикам" in ru_text.lower(), ru_text)


def test_data_requires_handle_and_accepts_pipe_or_comma() -> None:
    wallet = "0x" + "ab" * 20
    pipe, pipe_err = ud.parse_data_line(f"valid_name|{wallet}")
    comma, comma_err = ud.parse_data_line(f"otherName,{wallet}")
    assert pipe_err is None and pipe is not None and pipe.handle == "valid_name"
    assert comma_err is None and comma is not None and comma.handle == "otherName"

    wallet_only, wallet_only_err = ud.parse_data_line(wallet)
    assert wallet_only is None and wallet_only_err is not None
    long_handle, long_handle_err = ud.parse_data_line(f"abcdefghijklmnop|{wallet}")
    assert long_handle is None and long_handle_err is not None


def test_scheduler_respects_the_user_parallel_cap() -> None:
    """A user may run up to Caps.parallel_jobs at once, never more.

    The scheduler is the claim scan now: per-user running count and the
    per-project lane cap are enforced inside claim_next_job.
    """
    sq.reset_runtime_state()
    qj = sq.qj

    allowed = sq._user_parallel_jobs(7001)
    assert allowed >= 2, "default tier runs two projects at once"

    def _mk(uid: int, pid: str, status: str = "queued") -> int:
        return qj.create_job(
            user_id=uid, project_id=pid, chat_id=1, message_id=None,
            rows=[{"handle": "h", "wallet": "0x" + "11" * 20}],
        ) if status == "queued" else None

    # One running job -> the user's next project may be claimed too.
    r1 = _mk(7001, "p1")
    got1 = qj.claim_next_job(
        "sched1", user_parallel_fn=sq._user_parallel_jobs,
        project_limit_fn=sq._project_parallel_limit,
        project_claimable_fn=lambda p: True,
    )
    assert got1 is not None and got1["id"] == r1
    r2 = _mk(7001, "p2")
    got2 = qj.claim_next_job(
        "sched2", user_parallel_fn=sq._user_parallel_jobs,
        project_limit_fn=sq._project_parallel_limit,
        project_claimable_fn=lambda p: True,
    )
    assert got2 is not None and got2["id"] == r2, "second lane opens"

    # At the cap the next one waits, and another user is not blocked by it.
    while True:
        more = qj.claim_next_job(
            "schedx", user_parallel_fn=sq._user_parallel_jobs,
            project_limit_fn=sq._project_parallel_limit,
            project_claimable_fn=lambda p: True,
        )
        if more is None:
            break
        assert int(more["user_id"]) == 7001
    mine = _mk(7001, "p9")
    other = _mk(7002, "p3")
    got3 = qj.claim_next_job(
        "sched3", user_parallel_fn=sq._user_parallel_jobs,
        project_limit_fn=sq._project_parallel_limit,
        project_claimable_fn=lambda p: True,
    )
    assert got3 is not None and got3["id"] == other, (
        "a capped user must not block other users"
    )
    assert qj.load_job(mine)["status"] == "queued"
    sq.reset_runtime_state()


def test_restart_contract_is_lease_based_now() -> None:
    """A restart no longer marks interrupted jobs as bot_restart.

    Under the lease protocol an interrupted running job stays running until
    its lease expires; the next claim's sweep requeues it with running rows
    back to pending, and a worker resumes. Legacy payload-file jobs are the
    exception (one-time settlement, see test_boot_resume.py).
    """
    sq.init_schema()
    sq.reset_runtime_state()
    qj = sq.qj

    uid = 7003
    bot_db.ensure_user(uid, "restart", "restart")
    jid = qj.create_job(
        user_id=uid, project_id="democ", chat_id=0, message_id=None,
        rows=[{"handle": "tester", "wallet": "0x" + "aa" * 20}],
    )
    # Simulate a worker that died mid-job: claimed, row running, lease stale.
    # (Claim past any job an earlier test left open, then finish it.)
    claimed = None
    for _ in range(20):
        cand = qj.claim_next_job("deadworker")
        if cand is None:
            break
        if int(cand["id"]) == jid:
            claimed = cand
            break
        qj.finish_job(int(cand["id"]), "deadworker", int(cand["attempts"]),
                      "cancelled")
    assert claimed is not None and claimed["id"] == jid
    qj.mark_row_running(jid, 0, "deadworker", int(claimed["attempts"]))
    with bot_db.db() as conn:
        conn.execute(
            "UPDATE submit_jobs SET lease_until=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", jid),
        )
        conn.commit()

    sq.reconcile_zombie_jobs_on_boot()
    row = qj.load_job(jid)
    assert row["status"] == "running", (
        "boot must not touch a leased job; the lease decides"
    )
    # The next claim resumes it — same job, not a replay through the gates.
    resumed = qj.claim_next_job("freshworker")
    assert resumed is not None and resumed["id"] == jid
    assert int(resumed["attempts"]) == int(claimed["attempts"]) + 1
    seqs = [seq for seq, _r in qj.pending_rows(jid)]
    assert seqs == [0], "the interrupted row is pending again"
    qj.finish_job(jid, "freshworker", int(resumed["attempts"]), "cancelled")
    sq.reset_runtime_state()


def main() -> None:
    tests = [
        test_atomic_enqueue_rejects_duplicate,
        test_atomic_enqueue_concurrent_double_start,
        test_atomic_enqueue_cap_breach,
        test_runtime_disabled_rejected_at_enqueue,
        test_zero_slots_hides_start,
        test_will_send_capped_by_slots,
        test_no_proxies_hides_start_with_warning,
        test_health_config_hides_start,
        test_queue_start_markup_hidden_without_proxies,
        test_queue_start_markup_hidden_at_zero_slots,
        test_gate_strings_localized_en,
        test_data_requires_handle_and_accepts_pipe_or_comma,
        test_scheduler_respects_the_user_parallel_cap,
        test_restart_marks_jobs_error_without_replay,
    ]
    failed = 0
    for tfn in tests:
        try:
            tfn()
            print(f"PASS {tfn.__name__}")
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            failed += 1
            print(f"FAIL {tfn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
