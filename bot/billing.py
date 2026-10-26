"""Paid plan catalog and subscription state.

Tiers are per project, exactly like the free ones: the counter is
``user_project_stats.ok_count`` for one ``(user_id, project_id)`` pair, so
"100 wallets" means 100 successful registrations in *each* project.

Expiry is read-time, never a cron: :func:`active_plan` compares ``expires_at``
to now, so a bot that was down for a week needs no catch-up pass and no job can
forget to downgrade someone. The only background work is renewal reminders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from bot import config
from bot import db as bot_db


PLAN_FREE = "free"
PLAN_BASIC = "basic"
# There was a second paid tier ("pro") until 2026-08-17. The shop now sells one
# plan, so it is gone rather than hidden: a plan that exists in code but never
# in the price list is a thing that comes back by accident.

SETTING_MIN_AMOUNT = "np_min_amount_usd"
SETTING_MIN_CHECKED = "np_min_amount_checked_at"
MIN_AMOUNT_TTL_HOURS = 12


@dataclass(frozen=True)
class Plan:
    """One row of the price list. ``rank`` orders upgrades (higher = better)."""

    id: str
    rank: int
    price_usd: Decimal
    days: int
    ok_limit: int
    bulk_max_lines: int
    bulk_max_bytes: int
    job_parallel: int      # rows in flight inside ONE job
    parallel_jobs: int     # jobs of this user running at once
    may_upload_proxies: bool
    emoji: str


def _decimal(value: object, fallback: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return Decimal(fallback)


def _build_catalog() -> dict[str, Plan]:
    days = max(1, int(getattr(config, "PLAN_DAYS", 30)))
    return {
        PLAN_FREE: Plan(
            id=PLAN_FREE,
            rank=0,
            price_usd=Decimal("0"),
            days=0,
            ok_limit=0,  # resolved from bot_settings free_limit, not from here
            bulk_max_lines=int(config.BULK_MAX_LINES),
            bulk_max_bytes=int(config.BULK_MAX_BYTES),
            job_parallel=max(1, int(getattr(config, "BASE_JOB_PARALLEL", 1))),
            parallel_jobs=int(getattr(config, "USER_PARALLEL_JOBS", 2)),
            may_upload_proxies=True,
            emoji="🆓",
        ),
        PLAN_BASIC: Plan(
            id=PLAN_BASIC,
            rank=1,
            price_usd=_decimal(getattr(config, "PLAN_BASIC_PRICE_USD", "2"), "2"),
            days=days,
            ok_limit=int(getattr(config, "PLAN_BASIC_OK_LIMIT", 100)),
            bulk_max_lines=max(int(config.BULK_MAX_LINES), 200),
            bulk_max_bytes=max(int(config.BULK_MAX_BYTES), 200 * 1024),
            # Must stay above the unpaid tiers: raising BASE_JOB_PARALLEL to
            # drain the queue would otherwise make Basic buy nothing.
            job_parallel=max(
                3, int(getattr(config, "BASE_JOB_PARALLEL", 1)) + 1
            ),
            parallel_jobs=int(getattr(config, "PLAN_BASIC_PARALLEL_JOBS", 2)),
            may_upload_proxies=True,
            emoji="⭐",
        ),
    }


PLANS: dict[str, Plan] = _build_catalog()


def reload_catalog() -> dict[str, Plan]:
    """Rebuild from current config (admin /reload, tests)."""
    global PLANS
    PLANS = _build_catalog()
    return PLANS


def get_plan(plan_id: str | None) -> Plan:
    """Unknown or expired plan ids read as free — never raise on user input."""
    return PLANS.get(str(plan_id or "").strip().lower(), PLANS[PLAN_FREE])


def plan_catalog() -> list[Plan]:
    """Sellable plans, cheapest first."""
    return sorted(
        (p for p in PLANS.values() if p.id != PLAN_FREE and p.price_usd > 0),
        key=lambda p: p.rank,
    )


def plan_ok_limit(plan_id: str) -> int:
    """Per-project OK slots for a plan; an admin can override per plan in DB."""
    plan = get_plan(plan_id)
    if plan.id == PLAN_FREE:
        return int(bot_db.get_free_limit())
    raw = bot_db.get_setting(f"plan_limit:{plan.id}", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return int(plan.ok_limit)


def active_plan(user_id: int) -> tuple[str, str] | None:
    """``(plan_id, expires_at)`` while the subscription is still valid."""
    row = bot_db.get_subscription(int(user_id))
    if not row:
        return None
    plan_id = str(row.get("plan") or "")
    if plan_id not in PLANS or plan_id == PLAN_FREE:
        return None
    expires = bot_db.parse_iso(str(row.get("expires_at") or ""))
    if expires is None or expires <= datetime.now(timezone.utc):
        return None
    return plan_id, str(row.get("expires_at") or "")


def is_active(user_id: int) -> bool:
    return active_plan(int(user_id)) is not None


def days_left(user_id: int) -> int:
    """Whole days remaining, rounded up so the last partial day still counts."""
    current = active_plan(int(user_id))
    if current is None:
        return 0
    expires = bot_db.parse_iso(current[1])
    if expires is None:
        return 0
    seconds = (expires - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return 0
    return max(1, -(-int(seconds) // 86400))


def grant_plan(
    user_id: int,
    plan_id: str,
    *,
    months: int = 1,
    source: str = "admin",
    admin_id: int | None = None,
) -> tuple[bool, str]:
    """Manually add plan time (admin panel, support, compensation)."""
    uid = int(user_id)
    plan = get_plan(plan_id)
    if plan.id == PLAN_FREE:
        return False, "Неизвестный тариф"
    months = max(1, int(months))
    now = datetime.now(timezone.utc)
    current = active_plan(uid)
    start = now
    if current is not None and current[0] == plan.id:
        parsed = bot_db.parse_iso(current[1])
        if parsed is not None and parsed > now:
            start = parsed
    expires = (start + timedelta(days=plan.days * months)).isoformat()
    bot_db.upsert_subscription(
        uid, plan.id, expires, source=source, updated_by=admin_id
    )
    if admin_id is not None:
        bot_db.log_admin_action(
            admin_id,
            "plan_grant",
            target_user_id=uid,
            detail=f"{plan.id} +{plan.days * months}d until {expires}",
        )
    return True, expires


def revoke_plan(
    user_id: int, *, admin_id: int | None = None, reason: str = ""
) -> bool:
    uid = int(user_id)
    removed = bot_db.delete_subscription(uid)
    if removed and admin_id is not None:
        bot_db.log_admin_action(
            admin_id, "plan_revoke", target_user_id=uid, detail=reason[:200]
        )
    return removed


def min_amount_usd() -> Decimal | None:
    """Cached NOWPayments minimum in USD (None when never checked)."""
    raw = bot_db.get_setting(SETTING_MIN_AMOUNT, "").strip()
    if not raw:
        fallback = str(getattr(config, "NOWPAYMENTS_MIN_USD", "0")).strip()
        try:
            value = Decimal(fallback)
        except (InvalidOperation, ValueError):
            return None
        return value if value > 0 else None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def min_amount_is_stale() -> bool:
    checked = bot_db.parse_iso(bot_db.get_setting(SETTING_MIN_CHECKED, ""))
    if checked is None:
        return True
    age = datetime.now(timezone.utc) - checked
    return age > timedelta(hours=MIN_AMOUNT_TTL_HOURS)


def sales_open() -> bool:
    """False once the paid tier is closed to new buyers.

    Deliberately independent of plan state: subscriptions already sold keep
    working to their ``expires_at`` and ``grant_plan`` still works, so support
    and compensation are unaffected by a closed shop.
    """
    return bool(getattr(config, "SALES_ENABLED", True))


def is_purchasable(plan_id: str) -> bool:
    """False when the plan price is below the NOWPayments minimum.

    Only about the price: :func:`sales_open` is the shop switch, and mixing the
    two would make the admin panel report every plan as "below the minimum".
    """
    plan = get_plan(plan_id)
    if plan.id == PLAN_FREE:
        return False
    minimum = min_amount_usd()
    if minimum is None:
        return True
    return plan.price_usd >= minimum


def is_sellable(plan_id: str) -> bool:
    """What the buy buttons ask: the shop is open *and* the price clears."""
    return sales_open() and is_purchasable(plan_id)


def unpurchasable_plans() -> list[Plan]:
    return [p for p in plan_catalog() if not is_purchasable(p.id)]
