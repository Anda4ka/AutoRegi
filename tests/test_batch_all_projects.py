"""VIP «all projects» batch: one tap → one job per eligible project.

Covers:
  * queue depth: VIP cap rises to the project count, non-VIP stays at the base
  * the button is VIP-only and needs uploaded /data
  * eligibility: full / closed / already-queued projects are skipped
  * one batch per user, one shared dashboard message, one «stop everything»
  * cancel-all callbacks and RU/EN strings.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="batch_all_test_"))
_DB = _TMP / "test.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["JOB_PAYLOAD_DIR"] = str(_TMP / "job_payloads")

# Drop `tgbot` together with `bot.*`: this module repoints the state paths, and
# a `tgbot` left over from an earlier test module would keep its old `bot.db`
# binding — poisoning every module that imports tgbot after this one (whole-suite
# pytest runs, where the per-module CI runner would never notice).
for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import i18n  # noqa: E402
from bot import limits as bot_limits  # noqa: E402
from bot import submit_queue as sq  # noqa: E402
from bot import user_data as ud  # noqa: E402

config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]
config.JOB_PAYLOAD_DIR = _TMP / "job_payloads"

import tgbot  # noqa: E402

from bot.registry import list_projects, load_projects  # noqa: E402

load_projects()


def _assert(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    """Records what the batch would send/edit instead of calling Telegram."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self._next_id = 500

    def send_message(self, chat_id, text, **_kw):
        self._next_id += 1
        self.sent.append((chat_id, text))
        return FakeMessage(self._next_id)

    def edit_message_text(self, text, chat_id=None, message_id=None, **_kw):
        self.edited.append((chat_id, message_id, text))
        return FakeMessage(message_id or 0)


def _seed_user(uid: int, n_accounts: int = 3, *, vip: bool = False) -> None:
    bot_db.ensure_user(uid, "u", "U")
    data = "\n".join(
        f"user{uid}{i}|0x" + ("%02x" % ((uid + i) % 256)) * 20
        for i in range(n_accounts)
    )
    ud.save_user_data_from_bytes(uid, data.encode("utf-8"))
    if vip:
        bot_db.set_user_limit_override(uid, 1000, admin_id=1)
    bot_limits.invalidate(uid)


def _set_proxies(pool) -> None:
    # The shared pool lives in bot/proxy_pool.py since the phase-3 split.
    # Patch the module object tgbot holds (same generation as the code under
    # test): a fresh `from bot import proxy_pool` can resolve to a different
    # copy after another test module's purge, and the patch would silently
    # land on the wrong one.
    tgbot.proxy_pool._proxies_loaded = True
    tgbot.proxy_pool._proxies[:] = list(pool)


def _reset_queue() -> None:
    """Drop in-memory jobs between tests (no worker threads are started)."""
    sq.reset_runtime_state()


def _set_health(project_id: str, status: str) -> None:
    bot_db.set_setting(
        f"project_health:{project_id}",
        json.dumps({"project_id": project_id, "status": status}),
    )


def test_queue_depth_vip_only() -> None:
    bot_db.init_db()
    sq.init_schema()
    base = max(1, int(config.USER_MAX_ACTIVE_JOBS))
    plain = 20101
    vip = 20102
    _seed_user(plain, vip=False)
    _seed_user(vip, vip=True)

    _assert(sq._max_user_jobs() == base, "no user → base cap")
    _assert(sq._max_user_jobs(plain) == base, "non-VIP keeps the base cap")
    # auto (VIP_MAX_ACTIVE_JOBS=0) → as many jobs as there are projects
    want = max(base, len(list_projects()))
    _assert(sq._max_user_jobs(vip) == want, sq._max_user_jobs(vip))
    if len(list_projects()) > base:
        _assert(sq._max_user_jobs(vip) > base, "VIP must fit a whole batch")

    # An explicit env value wins over the project count.
    old = config.VIP_MAX_ACTIVE_JOBS
    try:
        config.VIP_MAX_ACTIVE_JOBS = 7
        _assert(sq._max_user_jobs(vip) == max(base, 7), sq._max_user_jobs(vip))
    finally:
        config.VIP_MAX_ACTIVE_JOBS = old


def test_button_is_vip_only_and_needs_data() -> None:
    bot_db.init_db()
    _set_proxies(["1.2.3.4:8080:u:p"])
    plain = 20201
    vip = 20202
    _seed_user(plain, vip=False)
    _seed_user(vip, vip=True)

    def _cb(kb, needle):
        return [
            b.callback_data
            for row in kb.keyboard
            for b in row
            if (b.callback_data or "") == needle
        ]

    kb_plain = tgbot.projects_keyboard("ru", user_id=plain)
    _assert(not _cb(kb_plain, "queue:all"), "non-VIP must not see the batch button")

    kb_vip = tgbot.projects_keyboard("ru", user_id=vip)
    _assert(_cb(kb_vip, "queue:all"), "VIP with /data must see the batch button")

    # No accounts uploaded → nothing to batch, button hidden.
    ud.clear_user_data(vip)
    kb_nodata = tgbot.projects_keyboard("ru", user_id=vip)
    _assert(not _cb(kb_nodata, "queue:all"), "batch button needs /data")
    _seed_user(vip, vip=True)


def test_eligible_skips_full_closed_and_queued() -> None:
    bot_db.init_db()
    _reset_queue()
    _set_proxies(["1.2.3.4:8080:u:p"])
    uid = 20301
    _seed_user(uid, n_accounts=3, vip=True)

    all_ids = [p.id for p in list_projects()]
    if len(all_ids) < 3:
        import pytest
        pytest.skip("batch skip-filters need several live projects")
    eligible = [p.id for p in tgbot.batch_eligible_projects(uid)]
    _assert(len(eligible) >= 3, eligible)

    # 1) health closed → out
    closed_id = eligible[0]
    _set_health(closed_id, "closed")
    # 2) limit reached → out (VIP limit is 1000 here, so use the override)
    full_id = eligible[1]
    bot_db.set_user_limit_override(uid, 1, admin_id=1)
    bot_limits.invalidate(uid)
    bot_db.record_submission(uid, full_id, "h", "0x" + "ab" * 20, "OK", "")

    after = [p.id for p in tgbot.batch_eligible_projects(uid)]
    _assert(closed_id not in after, f"{closed_id} closed but still eligible")
    _assert(full_id not in after, f"{full_id} full but still eligible")

    # restore VIP limit; only the closed project stays out
    bot_db.set_user_limit_override(uid, 1000, admin_id=1)
    bot_limits.invalidate(uid)
    _set_health(closed_id, "open")
    back = [p.id for p in tgbot.batch_eligible_projects(uid)]
    _assert(closed_id in back, "reopened project must come back")

    # 3) already queued → out (no duplicate job for the same project)
    queued_id = back[0]
    project = next(p for p in list_projects() if p.id == queued_id)
    job = sq.enqueue(
        user_id=uid,
        project=project,
        chat_id=1,
        message_id=None,
        rows=[{"x": "1"}],
    )
    _assert(queued_id not in [p.id for p in tgbot.batch_eligible_projects(uid)])
    sq.cancel_user_job(uid, job_id=job.id)
    _reset_queue()


def test_batch_starts_one_job_per_project_on_one_message() -> None:
    bot_db.init_db()
    _reset_queue()
    _set_proxies(["1.2.3.4:8080:u:p"])
    uid = 20401
    _seed_user(uid, n_accounts=2, vip=True)
    bot = FakeBot()

    eligible = tgbot.batch_eligible_projects(uid)
    ok, msg, started = tgbot.start_all_projects_batch(
        bot, user_id=uid, chat_id=77, lang="ru"
    )
    _assert(ok, msg)
    _assert(started == len(eligible), f"{started} != {len(eligible)}")

    jobs = sq.user_active_jobs(uid)
    _assert(len(jobs) == started, f"{len(jobs)} jobs for {started} projects")
    _assert(len({j.project_id for j in jobs}) == started, "one job per project")

    # All jobs share one batch id, one total and one dashboard message.
    _assert(len({j.batch_id for j in jobs}) == 1, "one batch id")
    _assert(all(j.batch_id for j in jobs), "batch jobs must be tagged")
    _assert(all(j.batch_total == started for j in jobs), "batch_total = started")
    _assert(len({j.message_id for j in jobs}) == 1, "one dashboard message")
    _assert(len(bot.sent) == 1, f"exactly one message posted, got {len(bot.sent)}")

    _assert(sq.user_batch_active(uid) is True)
    # Second tap must not stack a second batch.
    ok2, msg2, started2 = tgbot.start_all_projects_batch(
        bot, user_id=uid, chat_id=77, lang="ru"
    )
    _assert(not ok2 and started2 == 0, msg2)
    _assert(msg2 == i18n.t("queue.batch_busy", "ru"), msg2)
    _assert(len(sq.user_active_jobs(uid)) == started, "queue unchanged")

    # Dashboard: one summary with batch progress, one stop-everything button.
    summary = sq.format_user_jobs_summary(uid, lang="ru")
    _assert("Батч" in summary, summary)
    _assert(f"из {started}" in summary, summary)
    kb = sq.user_progress_keyboard(uid, lang="ru")
    cbs = [b.callback_data for row in kb.keyboard for b in row]
    _assert(cbs == ["qcancel:all"], cbs)

    _reset_queue()


def test_batch_needs_vip_and_reports_nothing_to_start() -> None:
    bot_db.init_db()
    _reset_queue()
    _set_proxies(["1.2.3.4:8080:u:p"])
    bot = FakeBot()

    plain = 20501
    _seed_user(plain, n_accounts=2, vip=False)
    ok, msg, started = tgbot.start_all_projects_batch(
        bot, user_id=plain, chat_id=1, lang="ru"
    )
    _assert(not ok and started == 0)
    _assert(msg == i18n.t("queue.batch_vip_only", "ru"), msg)
    _assert(not bot.sent, "a refused batch must not post a message")

    # VIP without proxies: every project fails readiness → nothing to start.
    vip = 20502
    _seed_user(vip, n_accounts=2, vip=True)
    _set_proxies([])
    ok2, msg2, started2 = tgbot.start_all_projects_batch(
        bot, user_id=vip, chat_id=1, lang="ru"
    )
    _assert(not ok2 and started2 == 0, msg2)
    _assert(msg2 == i18n.t("queue.batch_none", "ru"), msg2)
    _set_proxies(["1.2.3.4:8080:u:p"])
    _reset_queue()


def test_cancel_all_stops_every_job() -> None:
    bot_db.init_db()
    _reset_queue()
    _set_proxies(["1.2.3.4:8080:u:p"])
    uid = 20601
    _seed_user(uid, n_accounts=2, vip=True)
    bot = FakeBot()

    ok, _msg, started = tgbot.start_all_projects_batch(
        bot, user_id=uid, chat_id=88, lang="ru"
    )
    if started < 2:
        import pytest
        pytest.skip("cancel-all of a batch needs at least two live projects")
    _assert(ok and started >= 2, started)

    kb = sq.cancel_all_confirm_keyboard(uid, lang="ru")
    cbs = [b.callback_data for row in kb.keyboard for b in row]
    _assert("qcancel:allyes" in cbs and "qcancel:allno" in cbs, cbs)
    # Must not collide with the per-job or legacy prefixes.
    _assert(not any(c.startswith("queue:cancel") for c in cbs), cbs)

    n, last = sq.cancel_all_user_jobs(uid)
    _assert(n == started, f"{n} cancelled of {started}")
    _assert(last is not None)
    _assert(sq.user_active_jobs(uid) == [], "queue must be empty after stop all")
    _assert(sq.user_batch_active(uid) is False)
    # A new batch is allowed once the old one is gone.
    ok2, _m2, started2 = tgbot.start_all_projects_batch(
        bot, user_id=uid, chat_id=88, lang="ru"
    )
    _assert(ok2 and started2 > 0)
    _reset_queue()


def test_batch_claim_is_atomic() -> None:
    """Two parallel `queue:all` taps must produce one batch, not two."""
    uid = 987001
    import threading

    bot_db.ensure_user(uid, "batchrace", "B")
    outcomes: list[bool] = []

    def _tap() -> None:
        outcomes.append(sq.try_begin_user_batch(uid))

    threads = [threading.Thread(target=_tap) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    _assert(outcomes.count(True) == 1, f"exactly one claim wins: {outcomes}")
    # While claimed, even the read gate says busy.
    _assert(not sq.try_begin_user_batch(uid), "second claim refused")
    sq.end_user_batch(uid)
    _assert(sq.try_begin_user_batch(uid), "claim is releasable")
    sq.end_user_batch(uid)


def test_batch_strings_ru_en() -> None:
    keys = [
        "btn.queue_all",
        "btn.cancel_all_ask",
        "queue.batch_progress",
        "queue.more_jobs",
        "queue.batch_starting",
        "queue.batch_started",
        "queue.batch_skipped",
        "queue.batch_busy",
        "queue.batch_none",
        "queue.batch_vip_only",
        "queue.batch_cancel_confirm",
        "queue.batch_cancelled",
    ]
    for key in keys:
        entry = i18n.STRINGS.get(key)
        _assert(entry is not None, f"missing string {key}")
        for lang in ("ru", "en"):
            _assert(bool(entry.get(lang)), f"{key} missing {lang}")
    _assert("5" in i18n.t("btn.queue_all", "ru", n=5))
    _assert("3" in i18n.t("queue.batch_cancelled", "en", n=3))


def main() -> None:
    tests = [
        test_queue_depth_vip_only,
        test_button_is_vip_only_and_needs_data,
        test_eligible_skips_full_closed_and_queued,
        test_batch_starts_one_job_per_project_on_one_message,
        test_batch_needs_vip_and_reports_nothing_to_start,
        test_cancel_all_stops_every_job,
        test_batch_claim_is_atomic,
        test_batch_strings_ru_en,
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
