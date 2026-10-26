"""Tiered OK limits: free / subscriber / own-proxies / paid plan / VIP.

Precedence (highest wins):

1. ``/set_vip_limit`` DB override — verbatim, even below free (admin intent).
2. Hardcoded ``PERMANENT_USER_OK_LIMITS``.
3. ``max()`` of every applicable tier: the paid plan (basic) and the subscriber
   tier. Acquiring a tier can never lower a user. The subscriber tier itself
   is the **sum** of its two sources — ``active_subscribers.txt`` and
   membership in ``SUBSCRIBER_GROUP`` — so holding both is 50 + 50.
   Own proxies grant **no** limit anymore (operator rule, 2026-09-09):
   they only change the traffic source, never the OK count.
4. ``free_limit``.

Capability (bulk caps, queue parallelism) is deliberately split from identity:
``is_vip`` still means "admin override or hardcode", so a Basic plan does
not silently inherit VIP-sized bulk files and 5 threads.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from bot import billing
from bot import config
from bot import db as bot_db
from bot import group_tier
from bot import subscribers as sub
from bot import user_proxies as up


@dataclass(frozen=True)
class Caps:
    """Everything a tier grants, resolved once per user."""

    mode: str  # 'vip' | 'basic' | 'subscriber' | 'free'
    ok_limit: int
    bulk_max_lines: int
    bulk_max_bytes: int
    job_parallel: int      # rows in flight inside ONE job
    parallel_jobs: int     # jobs of this user running at once
    may_upload_proxies: bool
    has_own_proxies: bool
    plan: str = ""
    expires_at: str = ""


_cache: dict[int, tuple[float, Caps]] = {}
_cache_lock = threading.Lock()


def _cache_ttl() -> float:
    return max(0.0, float(getattr(config, "LIMITS_CACHE_TTL", 5) or 0))


def invalidate(user_id: int | None = None) -> None:
    """Drop one user (or everybody) from the tier cache."""
    with _cache_lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(int(user_id), None)


def is_vip(user_id: int) -> bool:
    """Admin-granted status only — a paid plan is NOT VIP."""
    uid = int(user_id)
    if bot_db.get_user_limit_override(uid) is not None:
        return True
    return uid in config.PERMANENT_USER_OK_LIMITS


def _resolve_caps(user_id: int) -> Caps:
    uid = int(user_id)
    has_own = up.has_own_proxies(uid)
    vip_lines = max(1, int(config.BULK_VIP_MAX_LINES))
    vip_bytes = max(1024, int(config.BULK_VIP_MAX_BYTES))
    base_lines = max(1, int(config.BULK_MAX_LINES))
    base_bytes = max(1024, int(config.BULK_MAX_BYTES))
    vip_parallel = max(1, int(config.VIP_JOB_PARALLEL))
    base_parallel = max(1, int(getattr(config, "BASE_JOB_PARALLEL", 1)))
    base_parallel_jobs = max(1, int(getattr(config, "USER_PARALLEL_JOBS", 2)))

    ovr = bot_db.get_user_limit_override(uid)
    hard = config.PERMANENT_USER_OK_LIMITS.get(uid)
    if ovr is not None or hard is not None:
        return Caps(
            mode="vip",
            ok_limit=int(ovr if ovr is not None else hard),
            bulk_max_lines=vip_lines,
            bulk_max_bytes=vip_bytes,
            job_parallel=vip_parallel,
            # VIP runs more project lanes at once than the paid plan: the
            # «all projects» batch is pointless if 17 queued jobs share three
            # lanes. This used to read the retired Pro plan's number; with that
            # plan gone `get_plan` would have answered with Free's, quietly
            # dropping VIP from three lanes to two.
            parallel_jobs=max(
                int(getattr(config, "VIP_PARALLEL_JOBS", 0) or 0),
                billing.get_plan(billing.PLAN_BASIC).parallel_jobs + 1,
            ),
            may_upload_proxies=True,
            has_own_proxies=has_own,
        )

    current = billing.active_plan(uid)
    plan_id, expires_at = current if current else ("", "")
    plan_limit = billing.plan_ok_limit(plan_id) if plan_id else 0
    # Own proxies no longer grant a tier (operator rule, 2026-09-09): before,
    # uploading a proxy file lifted a free user from 10 to 50 OK, which made
    # the 50-slot subscriber tier meaningless. They stay a traffic source
    # only — `has_own` below still routes resolve_proxies to the user's file.
    file_tier = (
        group_tier.subscriber_ok_limit()
        if sub.is_subscriber(uid)
        else 0
    )
    # Second source for the same tier: being in `SUBSCRIBER_GROUP` grants it
    # live, so nobody has to be typed into `active_subscribers.txt` by hand.
    grp_tier = (
        group_tier.member_ok_limit()
        if group_tier.enabled() and group_tier.is_member(uid)
        else 0
    )
    # The two subscriber sources ADD UP (operator, 2026-08-20): holding both —
    # in the file and in the private group — is 50 + 50 = 100. They are the
    # only pair that stacks; everything else stays a `max()`, because a plan
    # and a proxy upload are not two separate reasons to be trusted with more
    # slots, while these two are two separate ways of being the operator's
    # person. Leaving the group subtracts only what the group added.
    sub_tier = file_tier + grp_tier

    best = max(plan_limit, sub_tier)
    if best <= 0:
        return Caps(
            mode="free",
            ok_limit=int(bot_db.get_free_limit()),
            bulk_max_lines=base_lines,
            bulk_max_bytes=base_bytes,
            job_parallel=base_parallel,
            parallel_jobs=base_parallel_jobs,
            # Free may still upload own proxies: they no longer raise the
            # limit (2026-09-09) but remain a private traffic source, which
            # also takes load off the shared pool.
            may_upload_proxies=True,
            has_own_proxies=has_own,
        )

    # A paid plan drives capabilities whenever it is the winning tier; the
    # subscriber tier happening to be higher only lifts ok_limit.
    if plan_id and plan_limit >= best:
        plan = billing.get_plan(plan_id)
        return Caps(
            mode=plan.id,
            ok_limit=best,
            bulk_max_lines=plan.bulk_max_lines,
            bulk_max_bytes=plan.bulk_max_bytes,
            job_parallel=plan.job_parallel,
            parallel_jobs=max(plan.parallel_jobs, base_parallel_jobs),
            may_upload_proxies=plan.may_upload_proxies,
            has_own_proxies=has_own,
            plan=plan.id,
            expires_at=expires_at,
        )

    # Reachable only when the subscriber tier won the ladder (a paid plan is
    # handled above, own proxies grant nothing since 2026-09-09).
    return Caps(
        mode="subscriber",
        ok_limit=best,
        bulk_max_lines=base_lines,
        bulk_max_bytes=base_bytes,
        job_parallel=base_parallel,
        parallel_jobs=base_parallel_jobs,
        may_upload_proxies=True,
        has_own_proxies=has_own,
        plan=plan_id,
        expires_at=expires_at,
    )


def tier_caps(user_id: int, *, fresh: bool = False) -> Caps:
    """Resolved tier for this user, cached for ``LIMITS_CACHE_TTL`` seconds.

    Renders may use the cached value; submit gates must pass ``fresh=True``
    (``try_reserve_ok_slot`` stays the atomic authority either way).
    """
    uid = int(user_id)
    ttl = _cache_ttl()
    if not fresh and ttl > 0:
        with _cache_lock:
            hit = _cache.get(uid)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
    caps = _resolve_caps(uid)
    if ttl > 0:
        with _cache_lock:
            _cache[uid] = (time.monotonic(), caps)
    return caps


def limit_mode(user_id: int) -> str:
    """Return 'vip' | 'basic' | 'subscriber' | 'free' for UX."""
    return tier_caps(user_id).mode


def effective_ok_limit(
    user_id: int, project_ok_limit: int | None = None, *, fresh: bool = False
) -> int:
    """Effective OK slots for this user on any project.

    project_ok_limit is ignored (legacy arg kept for call-site compatibility).
    """
    del project_ok_limit  # ignored: gate is tier_caps(), see AGENTS.md §10
    return int(tier_caps(user_id, fresh=fresh).ok_limit)


# Modes that a captcha-backed project is offered to (plus any active plan via
# ``Caps.plan``). Every solve is money out of the operator's balance, so the
# free and subscriber tiers — which pay nothing — never see such a project.
# "vip" here means admin-granted; a basic plan alone is deliberately absent
# (operator rule, 2026-08-21) and enters only with the user's own solver key.
PAID_MODES = frozenset({"vip"})


def user_is_paid_or_vip(user_id: int | None) -> bool:
    """True for VIP and for anyone holding a paid plan right now.

    A paid plan that lost the ``max()`` ladder to a higher subscriber tier
    still counts: the user paid, and `Caps.mode` would read "subscriber" in
    that case, which would otherwise lock a payer out of what they bought.
    """
    if user_id is None:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    try:
        caps = tier_caps(uid)
        if caps.mode in PAID_MODES or caps.plan:
            return True
    except Exception:
        pass
    try:
        return billing.active_plan(uid) is not None
    except Exception:
        return False


def project_allowed(project, user_id: int | None) -> bool:
    """True when this user may see and submit to this project.

    Two independent gates:

    * ``requires_captcha`` — every row costs a paid solve. Two ways in:
      the operator's own solver balance (**VIP only**, `is_vip`: admin-granted,
      hardcoded ids and `/set_vip_limit`. Operator's rule, 2026-08-21 — a $50
      plan buys OK slots, not an open tap on somebody else's Capsolver/2Captcha
      account), **or the user's own solver key** uploaded via `/keys`
      (operator rule, 2026-09-08): their rows spend their balance only, never
      the operator's, so they enter at their normal tier with normal limits.
    * ``min_ok_limit`` — projects that mint wallets server-side cost far more
      per run than a plain form submit, so they can demand a tier floor
      instead of being offered to everyone.
    * ``only_user_ids`` — a form the operator runs for himself. It is checked
      first and it is absolute: no tier, plan or VIP flag opens a project
      pinned to someone else's id, because the reason for pinning is never
      "this tier cannot afford it" (measured 2026-08-19: one route verifies the
      repost link, so rows can only be filed for handles whose proof the
      operator supplies himself).
    """
    pinned = tuple(getattr(project, "only_user_ids", ()) or ())
    if pinned:
        if user_id is None:
            return False
        try:
            if int(user_id) not in pinned:
                return False
        except (TypeError, ValueError):
            return False
    if bool(getattr(project, "requires_captcha", False)):
        if user_id is None or not is_vip(int(user_id)):
            from bot import user_captcha

            if user_id is None or not user_captcha.has_key(int(user_id)):
                return False
    floor = int(getattr(project, "min_ok_limit", 0) or 0)
    if floor <= 0:
        return True
    if user_id is None:
        return False
    try:
        return effective_ok_limit(int(user_id)) >= floor
    except Exception:
        return False


def visible_projects(projects, user_id: int | None) -> list:
    """Filter a project list down to what this user is allowed to see."""
    return [p for p in projects if project_allowed(p, user_id)]


def effective_bulk_max_lines(user_id: int) -> int:
    """Max data lines accepted from one bulk .txt."""
    return max(1, int(tier_caps(user_id).bulk_max_lines))


def effective_bulk_max_bytes(user_id: int) -> int:
    """Max bulk .txt file size in bytes."""
    return max(1024, int(tier_caps(user_id).bulk_max_bytes))


def job_parallel(user_id: int) -> int:
    """Rows submitted in parallel inside one queue job."""
    return max(1, int(tier_caps(user_id).job_parallel))


def parallel_jobs(user_id: int) -> int:
    """How many of this user's queue jobs may run at the same time."""
    return max(1, int(tier_caps(user_id).parallel_jobs))


def may_upload_proxies(user_id: int) -> bool:
    """Every tier may upload own proxies — a private traffic source, not a limit."""
    return bool(tier_caps(user_id).may_upload_proxies)
