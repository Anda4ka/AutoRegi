"""Environment and path configuration for the multi-project bot."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
_EXPLICIT_STATE_ENV = os.getenv("AUTOREG_STATE_DIR", "").strip()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Public https URL of the Mini App (`webapp/`), or empty to hide its button.
# Empty is the default on purpose: unset it and the bot is exactly what it was
# before the prototype existed. Telegram refuses a web_app button on anything
# but https, so a non-https value is dropped rather than sent and rejected.
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
if WEBAPP_URL and not WEBAPP_URL.lower().startswith("https://"):
    WEBAPP_URL = ""

# Where the in-process mini app server listens. Local only: the sole route to
# it is the Cloudflare tunnel on this host, which is why no inbound port is
# open on the box at all.
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "127.0.0.1").strip() or "127.0.0.1"
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8081") or 8081)
# Set to 0 to serve the screen but refuse every action; the button and the
# read-only view keep working.
WEBAPP_ENABLE_ACTIONS = (os.getenv("WEBAPP_ENABLE_ACTIONS", "1").strip().lower()
                         not in ("0", "false", "no", "off"))
# Who may start and cancel from the web. Comma-separated ids, or `*` for
# everyone. Empty falls back to ADMIN_IDS — safe if nobody sets it, and not
# broken for the operator, who is the one testing it first.
WEBAPP_WRITE_USERS = os.getenv("WEBAPP_WRITE_USERS", "").strip()

# Runtime state must live outside the Git checkout. Clean deploys/clones replace
# BASE_DIR and Git intentionally does not restore ignored wallets, DBs or proxies.
_STATE_ENV = os.getenv("AUTOREG_STATE_DIR", "").strip()
_DB_ENV = os.getenv("BOT_DB", "").strip()
if _STATE_ENV:
    STATE_DIR = Path(_STATE_ENV).expanduser()
elif _DB_ENV:
    _db_hint = Path(_DB_ENV).expanduser()
    if not _db_hint.is_absolute():
        _db_hint = BASE_DIR / _db_hint
    STATE_DIR = _db_hint.parent
else:
    STATE_DIR = Path.home() / ".autoreg"


def _is_inside(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` itself or below it."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# Legacy-compat mode: a stale ``BOT_DB=tgbot.db`` (relative) with no
# ``AUTOREG_STATE_DIR`` pins STATE_DIR inside the checkout. In that case keep
# the per-user directories at their OLD in-repo locations (data/user_data …)
# so users' wallets do not silently vanish, and emit a loud warning. Tests and
# custom deployments that point BOT_DB outside the checkout are NOT legacy.
_LEGACY_COMPAT = (
    not _EXPLICIT_STATE_ENV
    and bool(_DB_ENV)
    and not Path(_DB_ENV).expanduser().is_absolute()
)


def _state_path(env_name: str, filename: str) -> Path:
    raw = os.getenv(env_name, "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path
    return STATE_DIR / filename


def _user_state_path(env_name: str, legacy_subdir: str, filename: str) -> Path:
    """Per-user dir: explicit env wins; legacy-compat keeps the old in-repo
    location (so wallets are found); otherwise the persistent STATE_DIR."""
    raw = os.getenv(env_name, "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else BASE_DIR / path
    if _LEGACY_COMPAT:
        return BASE_DIR / legacy_subdir
    return STATE_DIR / filename


PROXY_FILE = str(_state_path("PROXY_FILE", "proxy.txt"))
DB_PATH = _state_path("BOT_DB", "tgbot.db")
BOT_RESULTS = _state_path("BOT_RESULTS", "bot_results.jsonl")


def _parse_extra_proxy_files(raw: str) -> list[str]:
    """Comma/semicolon/newline-separated paths (relative to BASE_DIR or absolute)."""
    out: list[str] = []
    for part in (raw or "").replace(";", ",").replace("\n", ",").split(","):
        p = part.strip()
        if p:
            out.append(p)
    return out


# Additional shared proxy lists merged with PROXY_FILE (deduped URLs).
# Example: EXTRA_PROXY_FILES=message.txt,more_proxies.txt
EXTRA_PROXY_FILES: list[str] = _parse_extra_proxy_files(
    os.getenv("EXTRA_PROXY_FILES", "").strip()
)
# Shared pool kill switch. ``0`` = nobody may use proxy.txt (own /proxy only).
# SQLite ``shared_proxies_off`` is the live admin toggle; this env is the
# hard off that the panel cannot override.
SHARED_PROXIES = os.getenv("SHARED_PROXIES", "1").strip()

# Optional "paid private group" upsell screen (/priv). When empty the whole
# feature is hidden: no button, no command output. The bot itself never
# charges — it only points at this bot and reads group membership.
UPSELL_BOT = os.getenv("UPSELL_BOT", "").strip().lstrip("@")

# Legacy default when project.toml omits ok_limit (NOT used for submit gate after tiers)
OK_LIMIT = int(os.getenv("OK_LIMIT", "30"))

# Tier defaults — SQLite can override free at runtime (/set_free_limit).
# Own proxies grant no OK limit (operator rule, 2026-09-09): they only change
# the traffic source, never the count.
FREE_OK_LIMIT = int(os.getenv("FREE_OK_LIMIT", "10"))

# Bot-oriented defaults (GAS / rate-limited backends need longer retries)
MIN_DELAY = float(os.getenv("BOT_MIN_DELAY", "1"))
MAX_DELAY = float(os.getenv("BOT_MAX_DELAY", "2.5"))
RETRIES = int(os.getenv("BOT_RETRIES", "8"))
RETRY_DELAY = float(os.getenv("BOT_RETRY_DELAY", "10"))
# Queue workers and HTTP limits.
PROJECT_CONCURRENCY = max(1, int(os.getenv("PROJECT_CONCURRENCY", "3")))
GLOBAL_HTTP_CONCURRENCY = max(1, int(os.getenv("GLOBAL_HTTP_CONCURRENCY", "40")))
GLOBAL_REQUESTS_PER_MINUTE = max(1, int(os.getenv("GLOBAL_REQUESTS_PER_MINUTE", "100")))
BOT_CONCURRENCY = max(1, int(os.getenv("BOT_CONCURRENCY", str(GLOBAL_HTTP_CONCURRENCY))))
# pyTelegramBotAPI worker threads for handlers (UI + submit threads)
BOT_HANDLER_THREADS = int(os.getenv("BOT_HANDLER_THREADS", "10"))

# Per-user cooldown on incoming messages (seconds). One message per window;
# everything sent inside the window is dropped before any handler runs, so a
# flood costs one dict lookup instead of a DB read and a Telegram reply.
# 0 disables the gate. Admins are never gated. Inline buttons are not messages
# and stay free — the whole UI is buttons, and gating them would freeze it.
MESSAGE_COOLDOWN_SEC = max(0.0, float(os.getenv("MESSAGE_COOLDOWN_SEC", "60")))

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: set[int] = {
    int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()
}
# Dev-only: allow /admin when ADMIN_IDS empty (default fail-closed)
ALLOW_OPEN_ADMIN = os.getenv("ALLOW_OPEN_ADMIN", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Permanent VIP OK limits (hard-coded). /set_vip_limit DB overrides these.
# Seed extra entries without touching code via USER_OK_LIMITS=uid:limit,...
PERMANENT_USER_OK_LIMITS: dict[int, int] = {}

# Back-compat alias used by older call sites / env merge
_PERMANENT_USER_OK_LIMITS = PERMANENT_USER_OK_LIMITS


def _parse_user_ok_limits(raw: str) -> dict[int, int]:
    out = dict(PERMANENT_USER_OK_LIMITS)
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        uid_s, lim_s = part.split(":", 1)
        uid_s, lim_s = uid_s.strip(), lim_s.strip()
        if not uid_s.isdigit() or not lim_s.lstrip("-").isdigit():
            continue
        uid, lim = int(uid_s), int(lim_s)
        if lim < 0:
            continue
        # Env can raise; permanent floor for hardcode ids
        if uid in PERMANENT_USER_OK_LIMITS:
            out[uid] = max(PERMANENT_USER_OK_LIMITS[uid], lim)
        else:
            out[uid] = lim
    return out


# Env USER_OK_LIMITS seeds extra VIP-style overrides into permanent map for boot
USER_OK_LIMITS: dict[int, int] = _parse_user_ok_limits(
    os.getenv("USER_OK_LIMITS", "").strip()
)
# Merge env extras into permanent for is_vip hardcode path
PERMANENT_USER_OK_LIMITS = dict(USER_OK_LIMITS)


def effective_ok_limit(user_id: int, project_ok_limit: int | None = None) -> int:
    """Delegate to bot.limits (SQLite free limit + tiers + VIP)."""
    from bot.limits import effective_ok_limit as _impl

    return _impl(user_id, project_ok_limit)


def can_bulk_upload(user_id: int) -> bool:
    """Data bulk .txt is available to all users (PLAN PR3)."""
    del user_id
    return True


# Data bulk caps (unpaid-tier defaults; VIP uses BULK_VIP_* — see bot/limits.py)
BULK_MAX_BYTES = int(os.getenv("BULK_MAX_BYTES", str(100 * 1024)))
BULK_MAX_LINES = int(os.getenv("BULK_MAX_LINES", "100"))
# VIP can send larger bulk files (match VIP OK slots ~1000)
BULK_VIP_MAX_LINES = int(os.getenv("BULK_VIP_MAX_LINES", "1000"))
BULK_VIP_MAX_BYTES = int(os.getenv("BULK_VIP_MAX_BYTES", str(512 * 1024)))

# User proxy files (/proxy upload)
USER_PROXY_DIR = _user_state_path(
    "USER_PROXY_DIR", "data/user_proxies", "user_proxies"
)
# Allow large own lists (e.g. 1k proxies). Env can raise further.
USER_PROXY_MAX_LINES = int(os.getenv("USER_PROXY_MAX_LINES", "1000"))
USER_PROXY_MAX_BYTES = int(os.getenv("USER_PROXY_MAX_BYTES", str(512 * 1024)))

# User registration data (/data upload): handle|wallet lines
USER_DATA_DIR = _user_state_path("USER_DATA_DIR", "data/user_data", "user_data")
JOB_PAYLOAD_DIR = _user_state_path(
    "JOB_PAYLOAD_DIR", "data/job_payloads", "job_payloads"
)
USER_DATA_MAX_LINES = int(os.getenv("USER_DATA_MAX_LINES", "1000"))
USER_DATA_MAX_BYTES = int(os.getenv("USER_DATA_MAX_BYTES", str(512 * 1024)))

# /gform user-generated Google Form adapters. Outside the checkout on purpose:
# they are runtime state (a user's form), not code the repo ships — see §13.1.
# The registry scans this dir in addition to projects/.
USER_PROJECTS_DIR = _user_state_path(
    "USER_PROJECTS_DIR", "data/user_projects", "user_projects"
)
# One form = one project with its own tier limit (operator, 2026-09-10); this
# caps how many a single user may keep, so "form per project" cannot turn into
# an unlimited pile of fresh limits from one person.
MAX_USER_FORMS = int(os.getenv("MAX_USER_FORMS", "10"))

# Paid/active subscribers: higher OK limit while on shared proxy pool
# File: one Telegram user_id per line. Reload on start + admin /reload.
# Legacy and permanent: these ids keep their tier for good (grandfathered),
# paid plans in user_subscriptions are a separate, higher branch of the ladder.
SUBSCRIBERS_FILE = str(_state_path("SUBSCRIBERS_FILE", "active_subscribers.txt"))
SUBSCRIBER_OK_LIMIT = int(os.getenv("SUBSCRIBER_OK_LIMIT", "100"))

# Membership in this group grants the same tier, without anyone editing a file:
# every member of it gets SUBSCRIBER_OK_LIMIT OK slots per project, resolved
# live with getChatMember (bot/group_tier.py). The bot must be an
# administrator of the group, exactly like the channel gate. Empty = off.
_raw_group = os.getenv("SUBSCRIBER_GROUP", "").strip()
if _raw_group and not _raw_group.startswith("@") and not _raw_group.lstrip("-").isdigit():
    _raw_group = "@" + _raw_group.lstrip("@")
SUBSCRIBER_GROUP = _raw_group  # @username or numeric -100… chat id
# 0 = follow SUBSCRIBER_OK_LIMIT, so the two subscriber sources never drift.
SUBSCRIBER_GROUP_OK_LIMIT = int(os.getenv("SUBSCRIBER_GROUP_OK_LIMIT", "0") or 0)
# How long a membership answer is trusted. A "yes" is cheap to hold; a "no"
# expires fast, because the user reading it is joining the group right now.
GROUP_MEMBER_TTL = float(os.getenv("GROUP_MEMBER_TTL", "600") or 600)
GROUP_NONMEMBER_TTL = float(os.getenv("GROUP_NONMEMBER_TTL", "30") or 30)

# --- paid plans (bot/billing.py) ---
# Prices in USD; the customer always pays in USDT on BNB Smart Chain (BEP-20).
PLAN_BASIC_PRICE_USD = os.getenv("PLAN_BASIC_PRICE_USD", "50")
PLAN_DAYS = int(os.getenv("PLAN_DAYS", "30"))
PLAN_BASIC_OK_LIMIT = int(os.getenv("PLAN_BASIC_OK_LIMIT", "100"))
# How many of a user's jobs may RUN at once (queued cap stays
# USER_MAX_ACTIVE_JOBS). A slow project sleeps between rows, so a second job
# fills idle time rather than taking capacity from other users; the global
# HTTP semaphore and per-minute admission still bound everything.
USER_PARALLEL_JOBS = max(1, int(os.getenv("USER_PARALLEL_JOBS", "2")))
PLAN_BASIC_PARALLEL_JOBS = max(1, int(os.getenv("PLAN_BASIC_PARALLEL_JOBS", "2")))
# Tier cache: effective_ok_limit runs on every keyboard render (16 buttons =
# 16 calls), so a few seconds of staleness saves a lot of I/O. Submit gates
# always pass fresh=True; tests set 0.
LIMITS_CACHE_TTL = float(os.getenv("LIMITS_CACHE_TTL", "5"))

# --- NOWPayments (bot/payments_np.py) ---
PAYMENTS_ENABLED = os.getenv("PAYMENTS_ENABLED", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
# Sales switch, separate from PAYMENTS_ENABLED on purpose: closing sales must
# not stop the poller, or a transfer already on its way to an open address
# would never be credited. Off = no new payments, existing plans keep running
# to their expiry and an admin can still /grant_plan.
SALES_ENABLED = os.getenv("SALES_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)
# Mint calendar + X scanner. Hard off: the season is over, `/mint` `/mints`
# `/scan_mints` refuse, the mini-app tab is hidden, and no background scan
# starts. Flip to true (or MINTS_ENABLED=1) to bring the calendar back.
MINTS_ENABLED = os.getenv("MINTS_ENABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)

# --- NFT Trencher board feed (neverfuckingtrade.com) -> mint calendar ---
# The board's own API (docs: https://cdn.neverfuckingtrade.com/api/): mints a
# week ahead with per-stage times/prices, read from OpenSea and the chain.
# Access requires holding their Cassette NFT — the key is made on the site
# profile (Alerts & links → MAKE KEY) and dies if the Cassette is sold.
# Empty key = no sync; MINTS_ENABLED=0 still gates the whole calendar.
NFTT_API_KEY = os.getenv("NFTT_API_KEY", "").strip()
NFTT_BASE_URL = os.getenv(
    "NFTT_BASE_URL", "https://neverfuckingtrade.com/api/v1"
).rstrip("/")
# How far ahead of a stage the T-1h reminder fires (minutes).
MINT_REMIND_LEAD_MIN = int(os.getenv("MINT_REMIND_LEAD_MIN", "60"))

# While rolling out: only these ids see the paid UI (empty = everyone).
PAYMENTS_TEST_USER_IDS = {
    int(part)
    for part in os.getenv("PAYMENTS_TEST_USER_IDS", "").replace(";", ",").split(",")
    if part.strip().lstrip("-").isdigit()
}
NOWPAYMENTS_SANDBOX = os.getenv("NOWPAYMENTS_SANDBOX", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
NOWPAYMENTS_BASE_URL = os.getenv("NOWPAYMENTS_BASE_URL", "").strip()
NOWPAYMENTS_PAY_CURRENCY = os.getenv("NOWPAYMENTS_PAY_CURRENCY", "usdtbsc").strip()
NOWPAYMENTS_HTTP_TIMEOUT = float(os.getenv("NOWPAYMENTS_HTTP_TIMEOUT", "20"))
NOWPAYMENTS_POLL_INTERVAL = int(os.getenv("NOWPAYMENTS_POLL_INTERVAL", "20"))
NOWPAYMENTS_PAYMENT_TTL_HOURS = int(os.getenv("NOWPAYMENTS_PAYMENT_TTL_HOURS", "24"))
# --- captcha solver (bot/captcha.py) ---
# Keys for sites that actually verify their Turnstile token. A project that
# needs one names it in `health_required_env_any`, which makes it a
# `requires_captcha` project (paid/VIP only) — every row it files costs a solve.
# Two accounts because one running dry looks exactly like the site refusing the
# row: `solve_turnstile` walks them in order and steps over a provider that
# answered "no balance" or "bad key".
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "").strip()
TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
ANTICAPTCHA_API_KEY = os.getenv("ANTICAPTCHA_API_KEY", "").strip()
# Comma-separated provider names ("capsolver", "2captcha", "anticaptcha");
# unknown or unconfigured names are dropped, and any key left out of the order
# still counts. Users may bring their own key for any of the three via /keys —
# that spends their balance only, never these.
CAPTCHA_PROVIDER_ORDER = os.getenv(
    "CAPTCHA_PROVIDER_ORDER", "capsolver,2captcha,anticaptcha"
).strip()

# Auto-registration (users.auto_reg opt-in): a new project in the registry
# queues a run for every opted-in user with /data. 0 kills the feature
# without losing anyone's flag.
AUTO_REG_ENABLED = os.getenv("AUTO_REG_ENABLED", "1").strip()
# Boot resume: re-enqueue interrupted queued/running jobs from their persisted
# payloads through the normal enqueue_if_allowed gates, instead of erroring
# them as bot_restart. Off by default — the operator enables it deliberately.
BOOT_RESUME_JOBS = os.getenv("BOOT_RESUME_JOBS", "0").strip().lower() in ("1", "true", "yes")

# Fallback minimum when the live min-amount call fails: degrade open, let
# NOWPayments reject rather than hiding the product.
NOWPAYMENTS_MIN_USD = os.getenv("NOWPAYMENTS_MIN_USD", "0")
# Network fees can shave a little off the transfer; credit anyway above this.
PARTIAL_CREDIT_RATIO = float(os.getenv("PARTIAL_CREDIT_RATIO", "0.95"))
# Days-left buckets for renewal reminders.
SUB_REMIND_DAYS = tuple(
    sorted(
        {
            int(part)
            for part in os.getenv("SUB_REMIND_DAYS", "3,1").split(",")
            if part.strip().isdigit()
        },
        reverse=True,
    )
)

# --- UI ---
PROJECTS_PER_PAGE = max(1, int(os.getenv("PROJECTS_PER_PAGE", "8")))
# Telegram stops allowing edits around 48h; re-anchor before that.
SCREEN_MAX_AGE_HOURS = float(os.getenv("SCREEN_MAX_AGE_HOURS", "47"))


def _copy_file_if_missing(src: Path, dst: Path) -> int:
    """Copy one legacy state file atomically; never overwrite newer state."""
    if not src.is_file() or dst.exists() or src.resolve() == dst.resolve():
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.migrate-{os.getpid()}")
    try:
        import shutil

        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return 1
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _copy_sqlite_if_missing(src: Path, dst: Path) -> int:
    """Create a consistent SQLite backup atomically; never copy WAL/SHM raw."""
    if not src.is_file() or dst.exists() or src.resolve() == dst.resolve():
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.migrate-{os.getpid()}")
    try:
        # `closing` as well as the `with`: the latter ends the transaction and
        # leaves the descriptor open — the leak documented in `bot.db.db`. This
        # path runs once at startup, so it is hygiene rather than a bug.
        with closing(sqlite3.connect(src, timeout=30.0)) as source, \
                closing(sqlite3.connect(tmp, timeout=30.0)) as target:
            with source, target:
                source.backup(target)
                check = target.execute("PRAGMA quick_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    raise sqlite3.DatabaseError("legacy DB backup failed quick_check")
        tmp.chmod(0o600)
        os.replace(tmp, dst)
        return 1
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _copy_tree_missing(src_dir: Path, dst_dir: Path) -> int:
    """Copy legacy per-user files without replacing anything at destination."""
    if not src_dir.is_dir() or src_dir.resolve() == dst_dir.resolve():
        return 0
    copied = 0
    for src in src_dir.rglob("*"):
        if not src.is_file() or src.name == ".gitkeep":
            continue
        rel = src.relative_to(src_dir)
        copied += _copy_file_if_missing(src, dst_dir / rel)
    return copied


def _count_files(d: Path, suffix: str = ".txt") -> int:
    """Count regular files (default *.txt) in a flat directory."""
    if not d.is_dir():
        return 0
    try:
        return sum(1 for p in d.iterdir() if p.is_file() and p.suffix == suffix)
    except OSError:
        return 0


def prepare_state_layout() -> None:
    """
    Create persistent state directories and migrate the legacy in-repo layout.

    Migration runs when the operator is on the persistent layout
    (``AUTOREG_STATE_DIR`` set, or ``BOT_DB`` unset so STATE_DIR defaults to
    ~/.autoreg). It is intentionally skipped when ``BOT_DB`` is an arbitrary
    override (tests / custom setups) so a fresh temp DB is not silently
    replaced with a copy of production. In legacy-compat mode (stale relative
    ``BOT_DB``) the per-user directories stay at their old in-repo locations,
    so wallets keep being found — see ``_LEGACY_COMPAT``. Destination files
    always win, so repeated starts are safe.
    """
    for path in (
        STATE_DIR,
        DB_PATH.parent,
        BOT_RESULTS.parent,
        Path(PROXY_FILE).parent,
        Path(SUBSCRIBERS_FILE).parent,
        USER_PROXY_DIR,
        USER_DATA_DIR,
        JOB_PAYLOAD_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)

    migrate = (
        not _DB_ENV
        and os.getenv("AUTOREG_MIGRATE_LEGACY", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    copied = 0
    if migrate:
        legacy_db = BASE_DIR / "tgbot.db"
        if not DB_PATH.exists():
            copied += _copy_sqlite_if_missing(legacy_db, DB_PATH)
        if not os.getenv("BOT_RESULTS", "").strip():
            copied += _copy_file_if_missing(
                BASE_DIR / "bot_results.jsonl", BOT_RESULTS
            )
        if not os.getenv("PROXY_FILE", "").strip():
            copied += _copy_file_if_missing(BASE_DIR / "proxy.txt", Path(PROXY_FILE))
        if not os.getenv("SUBSCRIBERS_FILE", "").strip():
            copied += _copy_file_if_missing(
                BASE_DIR / "active_subscribers.txt", Path(SUBSCRIBERS_FILE)
            )
        if not os.getenv("USER_PROXY_DIR", "").strip():
            copied += _copy_tree_missing(
                BASE_DIR / "data" / "user_proxies", USER_PROXY_DIR
            )
        if not os.getenv("USER_DATA_DIR", "").strip():
            copied += _copy_tree_missing(
                BASE_DIR / "data" / "user_data", USER_DATA_DIR
            )
        if not os.getenv("JOB_PAYLOAD_DIR", "").strip():
            copied += _copy_tree_missing(
                BASE_DIR / "data" / "job_payloads", JOB_PAYLOAD_DIR
            )

    # Footgun warning: the state dir ended up INSIDE the Git checkout. This
    # happens with a stale ``BOT_DB=tgbot.db`` (relative) and no
    # ``AUTOREG_STATE_DIR`` — state is then wiped by a clean deploy/clone.
    if _LEGACY_COMPAT:
        print(
            "[state] WARNING: STATE_DIR is inside the Git checkout "
            f"({STATE_DIR}) because of a relative BOT_DB={_DB_ENV!r}. Wallets "
            f"are read from the legacy in-repo data/ dirs for now, but a clean "
            f"deploy/clone will lose them. Set AUTOREG_STATE_DIR (e.g. "
            f"/root/.autoreg) to move state outside the checkout.",
            flush=True,
        )

    legacy_ud = _count_files(BASE_DIR / "data" / "user_data")
    state_ud = _count_files(USER_DATA_DIR)
    print(
        f"[state] dir={STATE_DIR} db={DB_PATH} "
        f"user_data={USER_DATA_DIR} (legacy={legacy_ud}, state={state_ud}) "
        f"migrated={copied}",
        flush=True,
    )


prepare_state_layout()

# Project list UI: green check on inline button when user has this many OK on a project
PROJECT_BUTTON_OK_BADGE = int(os.getenv("PROJECT_BUTTON_OK_BADGE", "10"))

# Submit queue: max queued+running jobs per user (multi-project chain)
USER_MAX_ACTIVE_JOBS = int(os.getenv("USER_MAX_ACTIVE_JOBS", "3"))
# VIP only: queue depth for the «all projects» batch. One tap enqueues a job per
# eligible project, so the normal 3-job cap would refuse most of them. 0 = auto
# (as many jobs as there are enabled projects). Execution stays capped by
# bot.limits.parallel_jobs — a deeper queue does not mean more concurrent HTTP.
VIP_MAX_ACTIVE_JOBS = int(os.getenv("VIP_MAX_ACTIVE_JOBS", "0"))
# VIP only: how many of those queued jobs actually RUN at once. Queue depth
# without this is theatre — the «all projects» batch queues every project but
# ran three, so 20 row-threads sat idle behind a lane limit. 0 = inherit the Pro
# plan's value. Real ceilings above it stay GLOBAL_HTTP_CONCURRENCY and
# GLOBAL_REQUESTS_PER_MINUTE.
VIP_PARALLEL_JOBS = int(os.getenv("VIP_PARALLEL_JOBS", "0"))
# Parallel row submits inside one job for VIP only (non-VIP = 1)
VIP_JOB_PARALLEL = int(os.getenv("VIP_JOB_PARALLEL", "5"))
# Rows in flight inside one job for the unpaid tiers (free / own / subscriber).
# Was hardcoded to 1, which left 100 queue workers ~4% loaded: a job could only
# ever advance one row at a time, so queue depth was set by row latency rather
# than by any capacity limit. Paid tiers still rank above this (see billing).
BASE_JOB_PARALLEL = max(1, int(os.getenv("BASE_JOB_PARALLEL", "2")))

PROJECTS_DIR = Path(os.getenv("PROJECTS_DIR", str(BASE_DIR / "projects")))
PROJECTS_STRICT = os.getenv("PROJECTS_STRICT", "").strip() in ("1", "true", "yes")

# Required public channel subscription (getChatMember). Bot must be admin of the channel.
# Set REQUIRE_CHANNEL=0 to disable the check.
REQUIRE_CHANNEL = os.getenv("REQUIRE_CHANNEL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
_raw_ch = os.getenv("REQUIRED_CHANNEL", "").strip()
if _raw_ch and not _raw_ch.startswith("@") and not _raw_ch.lstrip("-").isdigit():
    _raw_ch = "@" + _raw_ch.lstrip("@")
REQUIRED_CHANNEL = _raw_ch  # @username or numeric -100… chat id
REQUIRED_CHANNEL_URL = os.getenv(
    "REQUIRED_CHANNEL_URL",
    "",
).strip()
# Display name in messages
REQUIRED_CHANNEL_TITLE = os.getenv(
    "REQUIRED_CHANNEL_TITLE",
    "",
).strip()
