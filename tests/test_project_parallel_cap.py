"""Per-project ceiling on rows in flight (`max_job_parallel`).

Tiers decide how many rows a user may submit at once; some endpoints cannot
take that number whatever the tier says. Google Apps Script sheets are the
case that forced this: they serialize writes behind a lock, so more threads
buy "Lock timeout" instead of speed — and a lost write can still answer with
the same redirect a successful one does, which is how the sheet-backed
projects ended up with
OK rows that are not in the sheet.

`queue_parallel` pins an exact number, `max_job_parallel` only ever lowers.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="parcap_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "t:t"
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
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
from bot.registry import list_projects, load_projects  # noqa: E402

config.DB_PATH = _TMP / "t.db"
bot_db.DB_PATH = _TMP / "t.db"  # type: ignore[attr-defined]


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class FakeProject:
    def __init__(self, *, queue_parallel=None, max_job_parallel=None):
        self.id = "fake"
        self.queue_parallel = queue_parallel
        self.max_job_parallel = max_job_parallel


def _plain_user(uid: int) -> int:
    bot_db.init_db()
    bot_db.ensure_user(uid, "u", "U")
    bot_limits.invalidate(uid)
    return uid


def _vip_user(uid: int) -> int:
    bot_db.init_db()
    bot_db.ensure_user(uid, "v", "V")
    bot_db.set_user_limit_override(uid, 1000, admin_id=1)
    bot_limits.invalidate(uid)
    return uid


def test_tier_values_come_through_unchanged() -> None:
    plain, vip = _plain_user(70101), _vip_user(70102)
    project = FakeProject()
    _ok(sq.resolve_job_parallel(project, plain) == 5, "unpaid tier = BASE_JOB_PARALLEL")
    _ok(sq.resolve_job_parallel(project, vip) == 20, "VIP tier = VIP_JOB_PARALLEL")


def test_ceiling_only_lowers() -> None:
    plain, vip = _plain_user(70201), _vip_user(70202)
    capped = FakeProject(max_job_parallel=3)
    _ok(sq.resolve_job_parallel(capped, vip) == 3, "VIP is capped down to the project")
    _ok(sq.resolve_job_parallel(capped, plain) == 3, "unpaid is capped down too")
    # A ceiling above the tier changes nothing — it is not an override.
    roomy = FakeProject(max_job_parallel=50)
    _ok(sq.resolve_job_parallel(roomy, plain) == 5, "ceiling must never raise a tier")
    _ok(sq.resolve_job_parallel(roomy, vip) == 20, "ceiling must never raise a tier")


def test_queue_parallel_pins_then_ceiling_applies() -> None:
    vip = _vip_user(70301)
    pinned = FakeProject(queue_parallel=8)
    _ok(sq.resolve_job_parallel(pinned, vip) == 8, "queue_parallel pins an exact value")
    both = FakeProject(queue_parallel=8, max_job_parallel=2)
    _ok(sq.resolve_job_parallel(both, vip) == 2, "ceiling wins over the pin")
    _ok(sq.resolve_job_parallel(FakeProject(max_job_parallel=0), vip) == 1, "never 0")


def test_sheet_backed_projects_stay_slow_for_vip() -> None:
    """A ceiling on the project must actually lower VIP's thread count."""
    vip = _vip_user(70401)
    capped = FakeProject(max_job_parallel=5)
    _ok(sq.resolve_job_parallel(capped, vip) == 5, "ceiling applies to VIP")
    uncapped = FakeProject()
    _ok(
        sq.resolve_job_parallel(uncapped, vip) == int(config.VIP_JOB_PARALLEL),
        "an uncapped project must give VIP the full tier",
    )


def test_vip_runs_many_project_lanes_at_once() -> None:
    """Queue depth is useless without lanes to run in.

    The «all projects» batch queues one job per project; before
    VIP_PARALLEL_JOBS it shared the paid plan's three lanes, so 17 queued jobs
    crawled three at a time and the 20 row-threads never showed up in practice.

    The floor used to be read off the retired Pro plan. With that plan gone
    `get_plan` would answer with Free's number, so the floor is now taken from
    the plan that is actually sold — a step above it, which is what «VIP beats
    the paid tier» has to mean when there is only one.
    """
    from bot import billing

    vip = _vip_user(70501)
    plain = _plain_user(70502)
    paid_lanes = billing.get_plan(billing.PLAN_BASIC).parallel_jobs
    floor = paid_lanes + 1
    want = max(int(getattr(config, "VIP_PARALLEL_JOBS", 0) or 0), floor)
    _ok(bot_limits.parallel_jobs(vip) == want, bot_limits.parallel_jobs(vip))
    # Never at or below the paid plan, whatever the knob says.
    _ok(bot_limits.parallel_jobs(vip) > paid_lanes,
        "VIP must outrun the tier people pay for")
    # Unpaid tiers are untouched by the VIP knob.
    _ok(
        bot_limits.parallel_jobs(plain) == int(config.USER_PARALLEL_JOBS),
        bot_limits.parallel_jobs(plain),
    )


def main() -> None:
    tests = [
        test_tier_values_come_through_unchanged,
        test_ceiling_only_lowers,
        test_queue_parallel_pins_then_ceiling_applies,
        test_sheet_backed_projects_stay_slow_for_vip,
        test_vip_runs_many_project_lanes_at_once,
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
