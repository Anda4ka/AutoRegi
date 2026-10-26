"""Paid plans: catalog, subscription state, tier precedence, crediting."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="billing_test_"))
_DB = _TMP / "t.db"
_PDIR = _TMP / "user_proxies"
_PDIR.mkdir(parents=True, exist_ok=True)
os.environ["BOT_DB"] = str(_DB)
os.environ["USER_PROXY_DIR"] = str(_PDIR)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["FREE_OK_LIMIT"] = "10"
os.environ["SUBSCRIBER_OK_LIMIT"] = "100"
os.environ["PLAN_BASIC_PRICE_USD"] = "50"
os.environ["PLAN_DAYS"] = "30"
os.environ["LIMITS_CACHE_TTL"] = "0"
os.environ["USER_OK_LIMITS"] = "333333333:1000"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import billing  # noqa: E402
from bot import billing_ux  # noqa: E402
from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import limits as bot_limits  # noqa: E402
from bot import subscribers as bot_subs  # noqa: E402
from bot import user_proxies as up  # noqa: E402
import bot.db as dbmod  # noqa: E402

config.DB_PATH = _DB
config.USER_PROXY_DIR = _PDIR
dbmod.DB_PATH = _DB

bot_db.init_db()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _pay(payment_id: str, uid: int, plan: str, amount: str) -> None:
    ok = bot_db.create_payment_record(
        payment_id=payment_id,
        order_id=f"order-{payment_id}",
        user_id=uid,
        plan=plan,
        price_amount=amount,
        pay_address="0x" + "a" * 40,
        pay_amount=amount,
    )
    _assert(ok, f"payment {payment_id} inserted")


def test_plan_catalog_prices_and_limits() -> None:
    basic = billing.get_plan("basic")
    _assert(float(basic.price_usd) == 50.0, "basic $50")
    _assert(basic.ok_limit == 100, "basic 100 per project")
    _assert(billing.get_plan("nonsense").id == "free", "unknown plan reads as free")
    _assert([p.id for p in billing.plan_catalog()] == ["basic"], "one paid plan")


def test_the_retired_plan_is_gone_not_hidden() -> None:
    """There was a second tier until 2026-08-17. It must not be sellable.

    A retired plan that still resolves is the one that comes back by accident —
    through an old callback, a stale keyboard, a copied config. `get_plan` is
    documented to read anything unknown as free, and that is what "pro" is now.
    """
    _assert(billing.get_plan("pro").id == "free", "retired plan reads as free")
    _assert("pro" not in [p.id for p in billing.plan_catalog()], "not in the catalogue")
    _assert(not billing.is_purchasable("pro"), "not purchasable")
    _assert(not billing.is_sellable("pro"), "not sellable")


def test_no_subscription_reads_as_free() -> None:
    uid = 700001
    _assert(billing.active_plan(uid) is None, "no plan")
    _assert(bot_limits.effective_ok_limit(uid) == bot_db.get_free_limit(), "free limit")
    _assert(bot_limits.limit_mode(uid) == "free", "mode free")


def test_expired_plan_reads_as_free() -> None:
    uid = 700002
    bot_db.upsert_subscription(uid, "pro", _iso(days=-1), source="admin")
    _assert(billing.active_plan(uid) is None, "expired plan is inactive")
    _assert(bot_limits.effective_ok_limit(uid) == bot_db.get_free_limit(), "back to free")
    _assert(bot_limits.limit_mode(uid) == "free", "mode free after expiry")


def test_active_plan_sets_limit_and_mode() -> None:
    uid = 700003
    ok, expires = billing.grant_plan(uid, "basic", admin_id=1)
    _assert(ok, f"grant basic: {expires}")
    _assert(bot_limits.effective_ok_limit(uid) == 100, "basic 100")
    _assert(bot_limits.limit_mode(uid) == "basic", "mode basic")
    _assert(billing.days_left(uid) == 30, f"30 days left, got {billing.days_left(uid)}")

    ok, _ = billing.grant_plan(uid, "nonsense", admin_id=1)
    _assert(not ok, "an unknown plan cannot be granted")
    _assert(bot_limits.limit_mode(uid) == "basic", "and leaves the real one alone")


def test_grant_stacks_when_still_active() -> None:
    uid = 700004
    billing.grant_plan(uid, "basic", admin_id=1)
    first = billing.days_left(uid)
    billing.grant_plan(uid, "basic", admin_id=1)
    second = billing.days_left(uid)
    _assert(second >= first + 29, f"second grant stacks: {first} → {second}")


def test_grant_after_expiry_starts_from_now() -> None:
    uid = 700005
    bot_db.upsert_subscription(uid, "basic", _iso(days=-10), source="admin")
    billing.grant_plan(uid, "basic", admin_id=1)
    _assert(billing.days_left(uid) == 30, f"restart from now, got {billing.days_left(uid)}")


def test_revoke_clears_plan() -> None:
    uid = 700006
    billing.grant_plan(uid, "basic", admin_id=1)
    _assert(bot_limits.effective_ok_limit(uid) == 100, "basic active")
    _assert(billing.revoke_plan(uid, admin_id=1), "revoked")
    _assert(billing.active_plan(uid) is None, "no plan after revoke")
    _assert(bot_limits.effective_ok_limit(uid) == bot_db.get_free_limit(), "free again")


def test_admin_override_beats_the_plan_verbatim() -> None:
    uid = 700007
    billing.grant_plan(uid, "basic", admin_id=1)
    bot_db.set_user_limit_override(uid, 25, admin_id=1)
    _assert(bot_limits.effective_ok_limit(uid) == 25, "override is verbatim, even below the plan")
    _assert(bot_limits.limit_mode(uid) == "vip", "mode vip")
    bot_db.del_user_limit_override(uid)
    _assert(bot_limits.effective_ok_limit(uid) == 100, "plan restored after override removal")


def test_hardcode_vip_beats_basic() -> None:
    uid = 333333333  # seeded below
    billing.grant_plan(uid, "basic", admin_id=1)
    _assert(bot_limits.effective_ok_limit(uid) == 1000, "hardcode VIP wins over basic")
    _assert(bot_limits.limit_mode(uid) == "vip", "mode vip")
    billing.revoke_plan(uid)


def test_legacy_subscriber_floor_not_lowered_by_basic() -> None:
    uid = 700008
    path = _TMP / "subs.txt"
    path.write_text(f"{uid}\n", encoding="utf-8")
    bot_subs.load_subscribers(path)
    _assert(bot_limits.effective_ok_limit(uid) == int(config.SUBSCRIBER_OK_LIMIT), "legacy 100")

    billing.grant_plan(uid, "basic", admin_id=1)
    _assert(
        bot_limits.effective_ok_limit(uid) >= int(config.SUBSCRIBER_OK_LIMIT),
        "basic never lowers a grandfathered subscriber",
    )
    billing.revoke_plan(uid)
    _assert(
        bot_limits.effective_ok_limit(uid) == int(config.SUBSCRIBER_OK_LIMIT),
        "legacy tier survives the paid plan ending",
    )
    bot_subs.load_subscribers(_TMP / "no_such_subs.txt")


def test_own_proxies_and_plan_take_max() -> None:
    uid = 700009
    ok, msg, _ = up.save_user_proxies_from_bytes(uid, b"1.2.3.4:8080:u:p\n")
    _assert(ok, msg)
    # Own proxies grant no tier (operator rule, 2026-09-09) — free stays free.
    _assert(bot_limits.effective_ok_limit(uid) == bot_db.get_free_limit(), "no own tier")
    billing.grant_plan(uid, "basic", admin_id=1)
    _assert(bot_limits.effective_ok_limit(uid) == 100, "the plan beats free proxies")
    _assert(bot_limits.limit_mode(uid) == "basic", "mode basic")
    billing.revoke_plan(uid)


def test_paid_plan_is_not_vip() -> None:
    uid = 700010
    billing.grant_plan(uid, "basic", admin_id=1)
    _assert(not bot_limits.is_vip(uid), "a paid plan must not read as admin VIP")
    billing.revoke_plan(uid)


def test_tier_capabilities() -> None:
    free_uid, basic_uid = 700011, 700012
    billing.grant_plan(basic_uid, "basic", admin_id=1)

    free = bot_limits.tier_caps(free_uid)
    basic = bot_limits.tier_caps(basic_uid)

    # The literal values are tunable (BASE_JOB_PARALLEL drains the queue), so
    # assert the ladder instead: raising the unpaid tier must never catch up
    # with the paid one, or the plan stops buying anything.
    _assert(
        free.job_parallel == int(config.BASE_JOB_PARALLEL),
        f"free follows BASE_JOB_PARALLEL, got {free.job_parallel}",
    )
    _assert(
        free.job_parallel < basic.job_parallel,
        f"job_parallel ladder broken: {free.job_parallel}/{basic.job_parallel}",
    )
    _assert(
        free.bulk_max_lines < basic.bulk_max_lines,
        "bulk caps grow with the tier",
    )

    # Every tier may upload own proxies: it is the path to the own tier (50).
    _assert(free.may_upload_proxies, "free may upload own proxies")
    _assert(basic.may_upload_proxies, "basic may upload own proxies")


def test_vip_keeps_its_lanes_without_a_second_plan() -> None:
    """VIP used to size its lanes off the retired Pro plan.

    `get_plan("pro")` answers with Free now, so reading that number would have
    quietly dropped VIP from three concurrent project lanes to two — the
    «запустить всё» batch is the thing that would have got slower, with nothing
    in the product changing to explain it.
    """
    vip_uid = 333333333  # seeded below
    caps = bot_limits.tier_caps(vip_uid, fresh=True)
    paid = billing.get_plan("basic").parallel_jobs
    _assert(
        caps.parallel_jobs > paid,
        f"VIP ({caps.parallel_jobs}) must outrun the paid plan ({paid})",
    )
    _assert(caps.parallel_jobs >= 3, f"three lanes was the floor, got {caps.parallel_jobs}")


def test_own_proxy_tier_still_reachable_from_free() -> None:
    uid = 700014
    ok, msg, _ = up.save_user_proxies_from_bytes(uid, b"1.2.3.4:8080:u:p\n")
    _assert(ok, msg)
    caps = bot_limits.tier_caps(uid)
    _assert(caps.mode == "free", "own proxies no longer lift the tier")
    _assert(caps.has_own_proxies, "but remain a traffic source")
    _assert(caps.may_upload_proxies, "and can be replaced later")


def test_credit_payment_grants_plan_once() -> None:
    uid = 700015
    _pay("pay-basic-1", uid, "basic", "2")
    ok, expires = bot_db.credit_payment_if_new("pay-basic-1")
    _assert(ok, f"credited: {expires}")
    _assert(bot_limits.effective_ok_limit(uid) == 100, "basic active after payment")

    again, reason = bot_db.credit_payment_if_new("pay-basic-1")
    _assert(not again and reason == "already_credited", "double finished credits once")
    _assert(billing.days_left(uid) == 30, "no extra days from the repeat")

    row = bot_db.get_payment_record("pay-basic-1")
    _assert(row is not None and row["status"] == "finished", "status finished")
    _assert(row["credited_at"], "credited_at stamped")


def test_credit_extends_existing_subscription() -> None:
    uid = 700016
    _pay("pay-basic-2", uid, "basic", "2")
    bot_db.credit_payment_if_new("pay-basic-2")
    first = billing.days_left(uid)
    _pay("pay-basic-3", uid, "basic", "2")
    bot_db.credit_payment_if_new("pay-basic-3")
    _assert(billing.days_left(uid) >= first + 29, "second payment stacks 30 more days")


# `test_credit_upgrade_carries_remaining_value` lived here until 2026-08-17.
# It exercised paying for a better plan while one was still running, and the
# carry-over of the remaining days at their cash value. With a single paid plan
# a user can only ever extend, which `test_credit_extends_existing_subscription`
# covers. The carry-over code in `bot/db.credit_payment_if_new` is still there
# and is now unreachable from the product — if a second tier ever returns, that
# test has to come back with it.


def test_concurrent_credit_credits_once() -> None:
    uid = 700018
    _pay("pay-race-1", uid, "basic", "50")
    results: list[tuple[bool, str]] = []
    lock = threading.Lock()

    def worker() -> None:
        out = bot_db.credit_payment_if_new("pay-race-1")
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [r for r in results if r[0]]
    _assert(len(results) == 4, "all threads returned")
    _assert(len(granted) == 1, f"exactly one credit, got {len(granted)}")
    _assert(billing.days_left(uid) == 30, "no double extension under a race")


def test_open_payment_lookup_and_terminal_states() -> None:
    uid = 700019
    _pay("pay-open-1", uid, "basic", "2")
    open_row = bot_db.get_open_payment_for_user(uid)
    _assert(open_row is not None and open_row["payment_id"] == "pay-open-1", "open payment found")

    bot_db.update_payment_status("pay-open-1", "expired")
    _assert(bot_db.get_open_payment_for_user(uid) is None, "terminal payment is not offered again")

    row = bot_db.get_payment_record("pay-open-1")
    _assert(int(row["poll_attempts"]) == 1, "poll attempt counted")
    _assert(row["last_poll_at"], "last_poll_at stamped")


def test_pollable_payments_window() -> None:
    uid = 700020
    _pay("pay-poll-1", uid, "pro", "10")
    fresh = bot_db.list_pollable_payments(cutoff_iso=_iso(hours=-26))
    _assert(any(p["payment_id"] == "pay-poll-1" for p in fresh), "open payment is pollable")

    stale = bot_db.list_pollable_payments(cutoff_iso=_iso(hours=1))
    _assert(all(p["payment_id"] != "pay-poll-1" for p in stale), "cutoff excludes old payments")

    bot_db.credit_payment_if_new("pay-poll-1")
    after = bot_db.list_pollable_payments(cutoff_iso=_iso(hours=-26))
    _assert(
        all(p["payment_id"] != "pay-poll-1" for p in after),
        "credited payments leave the poll set",
    )


def test_limits_cache_ttl_and_invalidate() -> None:
    uid = 700021
    os.environ["LIMITS_CACHE_TTL"] = "60"
    config.LIMITS_CACHE_TTL = 60.0
    try:
        bot_limits.invalidate()
        _assert(bot_limits.effective_ok_limit(uid) == bot_db.get_free_limit(), "free cached")

        # Written straight to the table: no hook, so the cache must still be stale.
        with dbmod.db() as conn:
            conn.execute(
                """
                INSERT INTO user_subscriptions
                    (user_id, plan, expires_at, source, started_at, updated_at)
                VALUES (?, 'basic', ?, 'test', ?, ?)
                """,
                (uid, _iso(days=30), _iso(), _iso()),
            )
            conn.commit()
        _assert(
            bot_limits.effective_ok_limit(uid) == bot_db.get_free_limit(),
            "cached value survives an unhooked write",
        )
        _assert(
            bot_limits.effective_ok_limit(uid, fresh=True) == 100,
            "fresh=True bypasses the cache (submit gates use this)",
        )
        bot_limits.invalidate(uid)
        _assert(bot_limits.effective_ok_limit(uid) == 100, "invalidate refreshes")
    finally:
        os.environ["LIMITS_CACHE_TTL"] = "0"
        config.LIMITS_CACHE_TTL = 0.0
        bot_limits.invalidate()


def test_submit_gates_read_the_tier_fresh() -> None:
    """Every slot reservation in tgbot.py must read the limit with fresh=True.

    ``try_reserve_ok_slot`` takes the limit as its bound, so a cached value
    becomes the ceiling the atomic gate enforces. Plan expiry is a time-based
    tier change: nothing calls ``invalidate()`` when the clock crosses
    ``expires_at``, so without ``fresh=True`` a Basic plan that expired
    seconds ago still admits rows at limit 100 for LIMITS_CACHE_TTL seconds.
    The render call sites stay cached on purpose — this pins only the three
    reservation gates (queue row, bulk file, direct text).
    """
    src = "\n".join(
        (ROOT / f).read_text(encoding="utf-8")
        for f in ["tgbot.py", "handlers/text.py", "handlers/documents.py"]
    )
    lines = src.splitlines()
    gates = [i for i, ln in enumerate(lines) if "try_reserve_ok_slot(" in ln]
    _assert(len(gates) == 3, f"expected 3 reservation gates, found {len(gates)}")
    for i in gates:
        lim_lines = [
            j for j in range(max(0, i - 200), i)
            if "effective_ok_limit(" in lines[j]
        ]
        _assert(lim_lines, f"reservation gate at tgbot.py:{i + 1} reads no tier limit")
        _assert(
            "fresh=True" in lines[lim_lines[-1]],
            f"reservation gate at tgbot.py:{i + 1} uses the limit from "
            f"tgbot.py:{lim_lines[-1] + 1} without fresh=True — a stale tier "
            "becomes the atomic bound",
        )


def test_min_amount_gate() -> None:
    _assert(billing.is_purchasable("basic"), "purchasable with no known minimum")
    bot_db.set_setting(billing.SETTING_MIN_AMOUNT, "5")
    _assert(billing.is_purchasable("basic"), "$50 clears a $5 minimum")
    _assert(billing.unpurchasable_plans() == [], "nothing flagged")
    # A provider minimum above the only price closes the shop by arithmetic,
    # which is the case worth keeping a test for: with one plan there is no
    # cheaper alternative to fall back to.
    bot_db.set_setting(billing.SETTING_MIN_AMOUNT, "100")
    _assert(not billing.is_purchasable("basic"), "$50 below a $100 minimum")
    _assert([p.id for p in billing.unpurchasable_plans()] == ["basic"], "basic flagged")
    bot_db.set_setting(billing.SETTING_MIN_AMOUNT, "0")
    _assert(billing.is_purchasable("basic"), "restored")


def test_sales_closed_keeps_existing_plans() -> None:
    """Closing the shop stops buying and nothing else."""
    uid = 9911001
    bot_db.ensure_user(uid, "closed", "Closed")
    billing.grant_plan(uid, billing.PLAN_BASIC, source="test")
    bot_limits.invalidate(uid)
    previous = config.SALES_ENABLED
    try:
        config.SALES_ENABLED = False
        _assert(not billing.sales_open(), "shop reports closed")
        _assert(not billing.is_sellable("basic"), "nothing is sellable")
        _assert(
            billing.is_purchasable("basic"),
            "is_purchasable stays about price, not about the shop",
        )

        # The people who already paid are untouched.
        _assert(billing.is_active(uid), "an existing plan stays active")
        caps = bot_limits.tier_caps(uid, fresh=True)
        _assert(caps.mode == "basic", f"plan still drives the tier, got {caps.mode}")
        _assert(caps.ok_limit == billing.plan_ok_limit("basic"), "limit unchanged")

        # And an admin can still hand out or take back time.
        other = 9911002
        bot_db.ensure_user(other, "granted", "Granted")
        ok, _detail = billing.grant_plan(other, billing.PLAN_BASIC, source="test")
        _assert(ok, "grant_plan works with the shop closed")
        _assert(billing.revoke_plan(other), "revoke_plan works too")

        # No buy buttons anywhere, and the screen says why.
        kb = billing_ux.plans_keyboard(uid, "ru")
        buttons = [b for row in kb.keyboard for b in row]
        _assert(
            not any((b.callback_data or "").startswith("plan:buy:") for b in buttons),
            "no buy button while closed",
        )
        text = billing_ux.plans_text(uid, "ru")
        _assert("закрыта" in text, "plans screen states the shop is closed")
        _assert("$50" not in text, "no price list while closed")
        dead = {"payment_id": "p1", "plan": "basic", "status": "expired"}
        retry = [b for row in billing_ux.payment_keyboard(dead, "ru").keyboard for b in row]
        _assert(
            not any((b.callback_data or "").startswith("plan:buy:") for b in retry),
            "a dead payment offers no retry while closed",
        )
        # Reopening is just the flag going back — set it explicitly rather than
        # trusting the ambient value, which follows the operator's live .env.
        config.SALES_ENABLED = True
        _assert(billing.sales_open(), "reopening restores the shop")
        _assert("$50" in billing_ux.plans_text(uid, "ru"), "price list is back")
    finally:
        config.SALES_ENABLED = previous
        billing.revoke_plan(uid)
        bot_limits.invalidate()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
