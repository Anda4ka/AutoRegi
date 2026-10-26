"""
Multi-project Telegram bot.

Flow: /start → pick project (inline) → send data → module submits.
Projects live under projects/<id>/ (see AGENTS.md).
Config: BOT_TOKEN in .env
"""

from __future__ import annotations

import html
import os
import logging
import re
import random
import uuid
import sys
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import telebot
from telebot import apihelper, types

# Telegram long-poll: Bot API holds the socket for long_polling_timeout seconds.
# HTTP read timeout must be clearly larger (apihelper uses max(long_poll+5, timeout)).
# Default combo timeout=60 / long_poll=50 is too tight → ReadTimeout on flaky links.
apihelper.CONNECT_TIMEOUT = 20
apihelper.READ_TIMEOUT = 45
apihelper.RETRY_ON_ERROR = True
apihelper.RETRY_TIMEOUT = 2
try:
    apihelper.MAX_RETRIES = 5
except Exception:
    pass

# Ensure repo root on path (projects.* imports)
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bot import admin_ux
from bot import alerts as bot_alerts
from bot import auto_reg as bot_auto_reg
from bot import billing
from bot import billing_ux
from bot import broadcast as bot_broadcast
from bot import bulk as bot_bulk
from bot import config, db as bot_db
from bot import proxy_check as bot_proxy_check
from bot import errors as bot_errors
from bot import form_closed as bot_form_closed
from bot import group_tier as bot_group_tier
from bot import limits as bot_limits
from bot import mints as bot_mints
from bot import mint_scan as bot_mint_scan
from bot import mint_ux
from bot import x_client as bot_x_client
from bot import new_project_alert as bot_new_project
from bot import project_health as bot_project_health
from bot import payments_np
from bot import payments_poll
from bot import rate_limit as bot_rate
from bot import screen
from bot import user_proxies as user_proxies_mod
from bot import user_captcha as user_captcha_mod
from bot import user_data as user_data_mod
from bot import submit_queue as bot_queue
from bot.http_throttle import SlidingWindowLimiter
from bot import subscribers as bot_subscribers
from bot import channel as bot_channel
from bot.channel import require_subscription, subscribe_keyboard, subscribe_text
from bot.i18n import (
    status_emoji,
    t,
)
from bot.registry import (
    Project,
    get_project,
    list_projects,
    load_projects,
    reload_projects,
)
from bot.ux import (
    esc,
    is_busy_detail,
    remaining,
    safe_edit_or_reply,
    safe_send,
)


# The mini app's capability seam. Importing it here (and never importing tgbot
# from webapp) is what keeps the dependency one-way: the web server is handed
# what it may do, it does not reach in and take it.
from webapp import actions as webapp_actions

# UI layer and the shared proxy pool moved to bot/ (phase-3 split);
# re-imported here so handlers, tests and admin paths keep the same names.
from bot import proxy_pool
from bot.proxy_pool import (  # noqa: F401
    _proxy_paths,
    get_proxies,
    load_bot_proxies,
    shared_enabled,
)
from bot.ui_screens import (  # noqa: F401
    ADMIN_PROJECTS_PER_PAGE,
    BULK_ERRORS_PER_PAGE,
    SUGGEST_MAX_LEN,
    SUGGEST_MIN_LEN,
    _bulk_report_key,
    _bulk_summary_text,
    _mint_keyboard,
    _project_button_label,
    _project_confirm_view,
    _project_mint_info,
    _project_start_state,
    _projects_per_page,
    _proxy_readiness,
    _queue_start_markup,
    _req_label_for_column,
    _screen_first_name,
    admin_keyboard_back,
    admin_keyboard_batch,
    admin_keyboard_health,
    admin_keyboard_home,
    admin_keyboard_limits,
    admin_keyboard_payments,
    admin_keyboard_projects,
    admin_keyboard_reload,
    admin_keyboard_user,
    bulk_report_load,
    bulk_report_save,
    default_result_message,
    language_keyboard,
    new_project_preview_keyboard,
    no_proxies_text,
    own_proxy_only_notice,
    partition_projects,
    project_ready_text,
    project_screen_keyboard,
    project_sort_key,
    projects_keyboard,
    projects_page,
    proxy_keyboard,
    proxy_status_text,
    queue_status_or_autostart,
    queue_status_or_prompt,
    register_screens,
    render_data,
    render_gate,
    render_home,
    render_lang,
    render_pay,
    render_plans,
    render_project,
    render_proxy,
    render_queue,
    render_result,
    render_suggest,
    status_text,
    suggest_prompt_keyboard,
    suggest_prompt_text,
    welcome_text,
)


_user_locks: dict[int, threading.Lock] = {}
_user_locks_guard = threading.Lock()
_submit_pool = threading.Semaphore(config.GLOBAL_HTTP_CONCURRENCY)
_http_rate_limiter = SlidingWindowLimiter(config.GLOBAL_REQUESTS_PER_MINUTE, 60.0)


# Await-mode state and the broadcast audience picker live in handlers/state.py
# (one lock per structure, shared by every handler group). tgbot re-imports
# the names so callers keep working.
from handlers.state import (  # noqa: F401
    BROADCAST_AUDIENCES,
    _awaiting_admin,
    _awaiting_admin_lock,
    _awaiting_data,
    _awaiting_data_lock,
    _awaiting_key,
    _awaiting_key_lock,
    _awaiting_proxy,
    _awaiting_proxy_lock,
    _awaiting_suggest,
    _awaiting_suggest_lock,
    _broadcast_draft,
    _clear_user_modes,
    _get_admin_await,
    _get_awaiting_key,
    _is_awaiting_data,
    _is_awaiting_proxy,
    _is_awaiting_suggest,
    _set_admin_await,
    _set_awaiting_data,
    _set_awaiting_key,
    _set_awaiting_proxy,
    _set_awaiting_suggest,
    broadcast_audience,
)


def count_own_proxy_users() -> int:
    d = config.USER_PROXY_DIR
    if not d.is_dir():
        return 0
    n = 0
    for p in d.glob("*.txt"):
        if p.name.startswith("."):
            continue
        try:
            uid = int(p.stem)
        except ValueError:
            continue
        if user_proxies_mod.has_own_proxies(uid):
            n += 1
    return n












def _notify_form_closed(project: Project, status: str, detail: str) -> None:
    """Alert admins when submit detail looks like form/WL closed (deduped)."""
    try:
        bot_form_closed.maybe_alert_form_closed(
            project_id=project.id,
            project_name=project.name,
            status=status,
            detail=detail or "",
            bot=_active_bot,
        )
    except Exception as e:
        print(f"[form_closed] notify err: {e}", flush=True)


def _admin_disable_project(admin_id: int, project_id: str) -> str:
    """
    Runtime-disable project + soft-cancel its queue jobs.
    Returns HTML status for admin message.
    """
    pid = (project_id or "").strip()
    p = get_project(pid, allow_runtime_disabled=True)
    name = p.name if p else pid
    if not pid:
        return "❌ empty project id"
    already = bot_db.is_project_runtime_disabled(pid)
    bot_db.set_project_runtime_disabled(pid, True, admin_id=admin_id)
    n_jobs = bot_queue.cancel_jobs_for_project(
        pid,
        detail="project disabled by admin",
        bot=_active_bot,
    )
    state = "уже был выключен" if already else "выключен"
    return (
        f"⏹ Проект <b>{esc(name)}</b> (<code>{esc(pid)}</code>) {state}.\n"
        f"Runtime (DB) · toml не меняли.\n"
        f"Queue soft-cancel: <b>{n_jobs}</b> job(s).\n"
        f"Включить: Админ → Projects → ▶ On."
    )


def _admin_enable_project(admin_id: int, project_id: str) -> str:
    pid = (project_id or "").strip()
    p = get_project(pid, allow_runtime_disabled=True)
    name = p.name if p else pid
    if not pid:
        return "❌ empty project id"
    if p is None:
        return (
            f"❌ <code>{esc(pid)}</code> не загружен "
            f"(toml enabled=false / нет module?) — runtime-флаг сбросим, "
            f"но кнопка появится только после enable в toml + /reload."
        )
    bot_db.set_project_runtime_disabled(pid, False, admin_id=admin_id)
    return (
        f"▶ Проект <b>{esc(name)}</b> (<code>{esc(pid)}</code>) включён.\n"
        f"Снова в /start."
    )














def check_new_projects() -> list[str]:
    """Offer admins an announcement for every registry id never announced.

    Detection reads the registry including runtime-disabled projects, so a
    project hidden by the closed-form alert does not later read as «new». It is
    simply not offered while it is off: an announcement pointing at a shut form
    is worse than a late one, so the id stays unmarked and comes back on the
    next reload.
    """
    try:
        ids = [p.id for p in list_projects(include_runtime_disabled=True)]
    except Exception as exc:  # noqa: BLE001 — never break a reload over this
        print(f"[newproj] registry read failed: {exc}", flush=True)
        return []

    with bot_new_project.notify_lock():
        return _announce_new_projects_locked(ids)


def _autoreg_start_job(*, user_id: int, chat_id: int, project):
    """Auto-reg entry into the normal queue path: same gates, same dashboard."""
    if _active_bot is None:
        return False, "bot not ready", None
    lang = bot_db.get_lang(user_id)
    return start_project_queue_job(
        _active_bot,
        user_id=user_id,
        chat_id=chat_id,
        project=project,
        lang=lang,
        auto_reg=True,
    )


def _announce_new_projects_locked(ids: list[str]) -> list[str]:
    fresh = bot_new_project.detect_new(ids)
    if not fresh:
        return []

    # Auto-registration runs first and regardless of the announcement switch:
    # the absorb path below marks ids as seen, and this is the only pass where
    # they are visible as new.
    for pid in fresh:
        if bot_db.is_project_runtime_disabled(pid):
            continue
        try:
            summary = bot_auto_reg.run_for_new_project(
                pid,
                start_job=_autoreg_start_job,
            )
            if summary.get("reason") == "ran" and _active_bot is not None:
                for admin_id in config.ADMIN_IDS:
                    try:
                        _active_bot.send_message(
                            admin_id,
                            "⚡ Авторег: <b>{name}</b> — в очередь поставлено "
                            "заданий: {n} (пропущено: {skipped})".format(
                                name=esc(pid), n=summary.get("queued", 0),
                                skipped=summary.get("skipped", 0),
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
        except Exception as exc:  # noqa: BLE001 — never break the reload over this
            print(f"[autoreg] sweep failed for {pid}: {exc}", flush=True)

    if not bot_new_project.is_enabled():
        # Switched off: absorb the ids so turning it back on does not blast a
        # backlog of everything added while it was quiet.
        for pid in fresh:
            bot_new_project.mark_announced(pid)
        return []

    if _active_bot is None or not config.ADMIN_IDS:
        return fresh

    offered: list[str] = []
    for pid in fresh:
        if bot_db.is_project_runtime_disabled(pid):
            continue
        project = get_project(pid)
        if project is None:
            continue
        try:
            recipients = len(bot_new_project.audience())
            text = bot_new_project.preview_text(project, recipients=recipients)
        except Exception as exc:  # noqa: BLE001
            print(f"[newproj] preview render failed for {pid}: {exc}", flush=True)
            continue
        kb = new_project_preview_keyboard(pid)
        delivered = False
        for admin_id in config.ADMIN_IDS:
            try:
                _active_bot.send_message(
                    admin_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
                delivered = True
            except Exception:
                continue
        if delivered:
            # Offered once. Whether the admin taps «Разослать» or ignores it,
            # this id is never proposed again.
            bot_new_project.mark_announced(pid)
            offered.append(pid)
            print(f"[newproj] offered {pid} to admins ({recipients} recipients)", flush=True)
    return offered


def _reload_runtime(*, admin_id: int | None = None) -> dict:
    """
    Hot-reload shared proxies, active_subscribers, and project modules.

    Does not re-read BOT_CONCURRENCY / BOT_HANDLER_THREADS / BOT_TOKEN (need process restart).
    Returns counts for admin messages / audit.
    """
    n_proxies = proxy_pool.reload_shared()
    n_sub = bot_subscribers.load_subscribers()
    # The group tier is answered by Telegram, not by a file, so a reload can
    # only drop what we remembered — the next question re-asks it.
    bot_group_tier.invalidate()
    projs = sorted(reload_projects(), key=lambda p: (p.order, p.name.lower()))
    names = ", ".join(p.id for p in projs) or "(none)"
    try:
        check_new_projects()
    except Exception as exc:  # noqa: BLE001 — a reload must not fail over an alert
        print(f"[newproj] check after reload failed: {exc}", flush=True)
    if admin_id is not None:
        bot_db.log_admin_action(
            admin_id,
            "reload",
            detail=f"proxies={n_proxies};subscribers={n_sub};projects={len(projs)}",
        )
        print(
            f"[admin] reload proxies={n_proxies} subscribers={n_sub} "
            f"projects={len(projs)} by {admin_id}",
            flush=True,
        )
    return {
        "proxies": n_proxies,
        "subscribers": n_sub,
        "projects": len(projs),
        "project_ids": names,
        "subscriber_ok_limit": int(config.SUBSCRIBER_OK_LIMIT),
    }


_project_watch_stop = threading.Event()


def _project_source_signature() -> tuple[tuple[str, int, int], ...]:
    """Return a cheap signature for project source files used by the bot."""
    root = Path(config.PROJECTS_DIR)
    files = []
    if root.is_dir():
        for path in root.glob("*/project.toml"):
            folder = path.parent
            if folder.name.startswith("_"):
                continue
            for source in (path, folder / "module.py", folder / "api.py"):
                if source.is_file():
                    stat = source.stat()
                    files.append((str(source), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(files))


def start_project_source_watcher(interval: float = 30.0) -> None:
    """Hot-reload project adapters when deployment changes their source files.

    Core bot changes still require a normal service restart. Project API/module
    changes are safe to reload in-process and queued jobs resolve the project
    at job start, so waiting jobs automatically use the new adapter.
    """
    _project_watch_stop.clear()

    def _watch() -> None:
        previous = _project_source_signature()
        while not _project_watch_stop.wait(max(5.0, float(interval))):
            try:
                current = _project_source_signature()
                if current == previous:
                    continue
                previous = current
                loaded = sorted(reload_projects(), key=lambda p: (p.order, p.name.lower()))
                print(
                    f"[projects] auto-reloaded {len(loaded)} project modules after source change",
                    flush=True,
                )
                check_new_projects()
            except Exception as exc:
                print(f"[projects] auto-reload failed: {exc}", flush=True)

    threading.Thread(target=_watch, name="project-source-watcher", daemon=True).start()


def _format_reload_result(info: dict) -> str:
    return (
        f"✅ <b>Reload</b>\n"
        f"Shared proxies: <b>{info['proxies']}</b>\n"
        f"Subscribers: <b>{info['subscribers']}</b> "
        f"(shared OK limit {info['subscriber_ok_limit']})\n"
        f"Projects: <b>{info['projects']}</b>\n"
        f"<code>{esc(info['project_ids'])}</code>"
    )


def _user_lock(user_id: int) -> threading.Lock:
    with _user_locks_guard:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


































def notify_admins_suggestion(
    bot: telebot.TeleBot,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    body: str,
    suggestion_id: int,
    utc_day: str,
) -> int:
    """Send suggestion to all ADMIN_IDS. Returns how many DMs succeeded."""
    if not config.ADMIN_IDS:
        print("[suggest] ADMIN_IDS empty — suggestion saved but not DM'd")
        return 0
    uname = f"@{esc(username)}" if username else "—"
    name = esc(first_name) if first_name else "—"
    text = (
        f"💡 <b>Новое предложение проекта</b> #{suggestion_id}\n"
        f"День (UTC): <code>{esc(utc_day)}</code>\n"
        f"От: {name} · {uname} · <code>{user_id}</code>\n\n"
        f"{esc(body)}"
    )
    ok_n = 0
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, parse_mode="HTML", disable_web_page_preview=True)
            ok_n += 1
        except Exception as e:
            print(f"[suggest] DM admin {admin_id} failed: {e}")
    return ok_n


















def do_submit(
    project: Project,
    fields: dict,
    *,
    user_id: int,
    queue_context: bool = False,
) -> dict:
    fields["_bot_user_id"] = int(user_id)
    queue_before_submit = getattr(project, "queue_before_submit", None)
    queue_after_submit = getattr(project, "queue_after_submit", None)
    if queue_context and queue_before_submit is not None:
        queue_before_submit(fields, user_id=user_id)
    health = bot_project_health.get_cached(project.id) or {}
    health_status = str(health.get("status") or "unknown")
    if health_status == "config":
        return {
            "status": "FAIL",
            "detail": "project_config_required: administrator setup needed",
            "handle": fields.get("handle"),
            "wallet": fields.get("wallet"),
        }
    if health_status == "closed":
        return {
            "status": "BAD_RESP",
            "detail": "registration closed (project health cache)",
            "handle": fields.get("handle"),
            "wallet": fields.get("wallet"),
        }
    shared = get_proxies()
    # Per-module bot tuning (registry); None → global BOT_* defaults.
    prefer_direct = bool(getattr(project, "prefer_direct", False))
    min_delay = (
        float(project.min_delay)
        if getattr(project, "min_delay", None) is not None
        else float(config.MIN_DELAY)
    )
    max_delay = (
        float(project.max_delay)
        if getattr(project, "max_delay", None) is not None
        else float(config.MAX_DELAY)
    )
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay
    retries = (
        int(project.retries)
        if getattr(project, "retries", None) is not None
        else int(config.RETRIES)
    )
    retry_delay = (
        float(project.retry_delay)
        if getattr(project, "retry_delay", None) is not None
        else float(config.RETRY_DELAY)
    )

    res = user_proxies_mod.resolve_proxies(
        user_id,
        allow_direct=project.allow_direct,
        shared=shared,
        is_vip_user=bot_limits.is_vip(user_id),
        prefer_direct=prefer_direct,
    )
    if res.source == "none" or res.source == "direct" or not (res.proxies or []):
        # AUD-001: never call project.submit without a real proxy pool.
        # "direct" is treated as forbidden even if resolve ever returns it.
        if res.source == "none" and user_proxies_mod.own_proxy_file_exists(user_id):
            detail = (
                "own_proxy_invalid: файл своих прокси битый — "
                "перезалей .txt в /proxy (shared не используется)"
            )
        else:
            detail = "no_proxies: загрузи свои прокси /proxy (общие недоступны)"
        return {
            "status": "FAIL",
            "detail": detail,
            "handle": fields.get("handle"),
            "wallet": fields.get("wallet"),
        }
    proxies = list(res.proxies or [])
    if not proxies:
        return {
            "status": "FAIL",
            "detail": "no_proxies: empty proxy pool",
            "handle": fields.get("handle"),
            "wallet": fields.get("wallet"),
        }
    proxy_idx = random.randint(0, max(len(proxies) - 1, 0))
    # Hold the global submit semaphore only around HTTP work as much as we can:
    # initial human-like delay is outside the pool so other users are not blocked
    # while we sleep (retry backoff still lives inside project adapters).
    if max_delay > 0:
        time.sleep(random.uniform(max(0.0, min_delay), max_delay))
    _http_rate_limiter.acquire()
    with _submit_pool:
        result = project.submit(
            fields,
            proxies=proxies,
            proxy_idx=proxy_idx,
            retries=retries,
            retry_delay=retry_delay,
            min_delay=0.0,
            max_delay=0.0,
        )
    if queue_context and queue_after_submit is not None:
        try:
            queue_after_submit(fields, result, user_id=user_id)
        except Exception:
            # The remote result is authoritative. A project-side bookkeeping
            # failure must never turn an accepted submission into a local FAIL.
            logging.getLogger(__name__).exception(
                "queue_after_submit failed project=%s user=%s",
                project.id,
                user_id,
            )
    return result


def queue_submit_one(project: Project, fields: dict, user_id: int) -> dict:
    """Used by submit_queue workers: submit + record DB (no Telegram reply)."""
    # Queue jobs already have an atomic per-(project, wallet) claim below.
    # Keeping the broad user lock around the whole HTTP request serialized all
    # parallel rows for the same user (and also serialized that user's
    # projects), making the tier's job_parallel ineffective.  Retain the lock
    # for single-threaded tiers, where per-user serialization is still the
    # conservative default; parallel tiers rely on wallet_claims for duplicate
    # protection.
    queue_lock = (
        _user_lock(user_id)
        if bot_limits.job_parallel(user_id) <= 1
        else nullcontext()
    )
    with queue_lock:
        handle = fields.get("handle") or fields.get("twitter")
        wallet = fields.get("wallet")
        # Global bot dedup: any user already OK/ALREADY for project+wallet
        prev = bot_db.already_registered_wallet(project.id, wallet)
        if prev:
            return {
                "status": "ALREADY",
                "detail": "bot_dedup_global",
                "handle": handle or prev.get("handle"),
                "wallet": wallet,
                "skip_record": True,
                "dedup": prev,
            }

        # AUD-007: claim before HTTP so parallel workers cannot double-submit
        claimed, creason = bot_db.try_claim_wallet(project.id, wallet, user_id)
        if not claimed:
            return {
                "status": "ALREADY" if creason == "already" else "FAIL",
                "detail": (
                    "bot_dedup_global"
                    if creason == "already"
                    else "wallet_claim_in_flight"
                ),
                "handle": handle,
                "wallet": wallet,
                "skip_record": True,
            }

        lim = bot_limits.effective_ok_limit(user_id, fresh=True)
        slot_reservation = bot_db.try_reserve_ok_slot(
            user_id,
            project.id,
            lim,
        )
        if not slot_reservation:
            bot_db.release_wallet_claim(project.id, wallet)
            return {
                "status": "SKIPPED",
                "detail": "ok_limit_reached",
                "handle": handle,
                "wallet": wallet,
                "skip_record": True,
                "skipped": True,
                "limit_reached": True,
            }

        try:
            result = do_submit(
                project,
                fields,
                user_id=user_id,
                queue_context=True,
            )
        except Exception:
            bot_db.release_wallet_claim(project.id, wallet)
            bot_db.release_ok_slot(slot_reservation)
            raise

        status = result.get("status", "FAIL")
        detail = result.get("detail", "")
        # FAIL too: a sheet lock that survives every retry comes back as FAIL,
        # and "server busy, try again" is the honest thing to show for it.
        if status in ("BAD_RESP", "FAIL") and is_busy_detail(detail):
            status = "BUSY"
            result["status"] = status
        handle = fields.get("handle") or fields.get("twitter") or result.get("handle")
        wallet = fields.get("wallet") or result.get("wallet") or wallet
        extra = {
            k: v
            for k, v in fields.items()
            if k not in ("handle", "wallet", "twitter") and not k.startswith("_")
        }
        if result.get("skip_record"):
            bot_db.release_wallet_claim(project.id, wallet)
            bot_db.release_ok_slot(slot_reservation)
            return result

        try:
            new_ok = bot_db.record_submission_and_finalize_claim(
                user_id,
                project.id,
                handle,
                wallet,
                status,
                detail,
                extra=extra or None,
                claim_wallet=wallet,
                slot_reservation_id=slot_reservation,
            )
        except Exception:
            # A failed record (SQLite busy, disk) must not strand the claim or
            # the OK-slot reservation — both would suppress later submits.
            bot_db.release_wallet_claim(project.id, wallet)
            bot_db.release_ok_slot(slot_reservation)
            raise
        bot_db.append_bot_result(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "project_id": project.id,
                "handle": handle,
                "wallet": wallet,
                "status": status,
                "detail": (detail or "")[:300],
                "ok_count": new_ok,
                "queue": True,
            }
        )
        _notify_form_closed(project, status, detail)
        result["ok_count"] = new_ok
        return result


def start_project_queue_job(
    bot: telebot.TeleBot,
    *,
    user_id: int,
    chat_id: int,
    project: Project,
    lang: str = "ru",
    auto_reg: bool = False,
) -> tuple[bool, str, object | None]:
    """
    Build rows from /data, enqueue job (chain up to USER_MAX_ACTIVE_JOBS).
    Returns (ok, message_html, job_or_none).

    ``auto_reg=True`` lifts the per-user queue cap to the number of live
    projects so a sweep can sit every project in the queue; execution is
    still ``parallel_jobs``. Duplicate-project / slots / proxies / dedup
    stay in force.

    Gate (duplicate / cap / runtime-disabled) is enforced atomically inside
    ``bot_queue.enqueue_if_allowed`` so two parallel callbacks cannot both
    insert (race under BOT_HANDLER_THREADS > 1).
    """
    if not bot_limits.project_allowed(project, user_id):
        return False, t("toast.project_unavailable", lang), None

    # Self-sourced projects mint their own rows — there is nothing to upload.
    if not project.self_sourced and not user_data_mod.has_user_data(user_id):
        return False, t("data.need", lang), None

    # Readiness gate (proxy + slots) BEFORE preparing rows. Keeps the project
    # screen, /queue and the actual start consistent: Start is only offered /
    # runnable when there are proxies to send through and slots left.
    st = _project_start_state(
        project, bot_db.get_ok_count(user_id, project.id), user_id
    )
    if st["health_blocked"]:
        key = (
            "project.health_config"
            if st["health_status"] == "config"
            else "project.health_closed"
        )
        return False, t(key, lang), None
    if st["proxy_src"] == "none" and not st["allow_direct"]:
        return False, no_proxies_text(lang, name=esc(project.name)), None
    if st["left"] <= 0:
        return (
            False,
            t("limit.exhausted", lang, name=esc(project.name), limit=st["lim"], extra=""),
            None,
        )

    # Best-effort pre-checks for nicer UX in the common case. These are NOT
    # authoritative — enqueue_if_allowed re-validates atomically.
    same = bot_queue.user_has_project_job(user_id, project.id)
    if same is not None:
        return (
            False,
            t(
                "queue.same_project",
                lang,
                id=same.id,
                project=esc(project.name),
                status=same.status,
            ),
            same,
        )

    if not auto_reg and not bot_queue.can_enqueue_user(user_id):
        nmax = bot_queue.max_user_jobs(user_id)
        active = bot_queue.user_has_active_job(user_id)
        summary = bot_queue.format_user_jobs_summary(user_id, lang=lang)
        return (
            False,
            t(
                "queue.full",
                lang,
                cur=bot_queue.user_active_job_count(user_id),
                max=nmax,
            )
            + ("\n\n" + summary if summary else ""),
            active,
        )

    ahead = bot_queue.user_active_jobs(user_id)
    rows, errs, skipped_dup = bot_queue.prepare_job_rows(user_id, project)
    if not rows:
        head = "❌ " + t("queue.nothing_to_queue", lang) + "\n"
        if errs:
            head += "\n".join(errs[:15])
        if skipped_dup:
            head += "\n" + t("dedup.queue_all", lang, n=skipped_dup)
        return False, head, None

    # Reuse the existing user's dashboard for chained projects. Only the first
    # project posts a message — and it is its own message, never the
    # navigation anchor, so starting a job does not eat the screen the user is
    # standing on.
    mid_is_new = False
    if ahead and ahead[0].message_id:
        mid = ahead[0].message_id
    else:
        try:
            pre = (
                f"⏳ {t('queue.running_title', lang)} · <b>{esc(project.name)}</b>\n"
                f"{t('queue.accounts', lang, n=len(rows))}"
            )
            msg = bot.send_message(chat_id, pre, parse_mode="HTML")
            mid = msg.message_id
            mid_is_new = True
        except Exception:
            mid = None

    # Authoritative atomic gate + insert.
    job, reason, existing = bot_queue.enqueue_if_allowed(
        user_id=user_id,
        project=project,
        chat_id=chat_id,
        message_id=mid,
        rows=rows,
        skipped_dup=skipped_dup,
        auto_reg=auto_reg,
    )
    if job is None:
        # Lost the race (or admin disabled the project) between pre-check and
        # insert. Repurpose the placeholder as the error message.
        if reason == "duplicate" and existing is not None:
            err_text = t(
                "queue.same_project",
                lang,
                id=existing.id,
                project=esc(project.name),
                status=existing.status,
            )
        elif reason == "disabled":
            err_text = t("queue.project_disabled", lang, name=esc(project.name))
        else:
            nmax = bot_queue.max_user_jobs(user_id)
            err_text = t(
                "queue.full",
                lang,
                cur=bot_queue.user_active_job_count(user_id),
                max=nmax,
            )
        if mid is not None and mid_is_new:
            # Repurpose the placeholder we just posted as the error message.
            # Never edit `mid` borrowed from a live job's dashboard: the
            # worker keeps redrawing it and the two would fight over the
            # message — the refusal goes to the caller's screen instead.
            try:
                bot.edit_message_text(
                    err_text,
                    chat_id=chat_id,
                    message_id=mid,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        return False, err_text, existing
    job.project_name = project.name
    bot_queue.update_job_message(bot, job, lang=lang)
    note = ""
    if ahead:
        note = ""
    if skipped_dup:
        note += "\n\n" + t("dedup.queue_note", lang, n=skipped_dup)
    if errs:
        note += "\n\n" + t("queue.skipped_lines_note", lang, n=len(errs))
    return (
        True,
        bot_queue.format_job_status(job, lang=lang) + note,
        job,
    )


def batch_eligible_projects(user_id: int) -> list[Project]:
    """Projects the «all projects» button would actually start for this user.

    Only live and unfinished ones: a project the user has already filled up,
    one the health checker calls closed/misconfigured, one without proxies to
    send through, and one already sitting in this user's queue are all skipped
    rather than queued to fail. Never-checked projects (health ``unknown``) are
    kept — a fresh process has no health map yet, and dropping them would make
    the button do nothing right after a restart.
    """
    projects = bot_limits.visible_projects(list_projects(), user_id)
    data_rows = user_data_mod.load_user_data_rows(user_id)
    wallets_lower = {(r.wallet or "").strip().lower() for r in data_rows}
    out: list[tuple] = []
    for p in projects:
        if bot_queue.user_has_project_job(user_id, p.id) is not None:
            continue
        ok_n = bot_db.get_ok_count(user_id, p.id)
        st = _project_start_state(p, ok_n, user_id, rows=data_rows,
                                  wallet_set=wallets_lower)
        if not st["can_start"]:
            continue
        out.append((p, ok_n, st["health_status"], st.get("fresh")))
    out.sort(key=lambda item: project_sort_key(item[0], item[1], item[2], item[3]))
    return [p for p, _ok, _status, _fresh in out]


def start_all_projects_batch(
    bot: telebot.TeleBot,
    *,
    user_id: int,
    chat_id: int,
    lang: str = "ru",
) -> tuple[bool, str, int]:
    """VIP: enqueue one job per eligible project from a single tap.

    Returns ``(ok, message_html, started)``. Every job of the batch shares one
    dashboard message (`format_user_jobs_summary`) and one «stop everything»
    button, so twenty projects cost the user one message, not twenty.

    The per-project gates are the same ones the single-project Start uses —
    this only saves taps, it does not lift a limit: slots, dedup and the OK
    limit are still enforced per project inside the queue.
    """
    if not bot_limits.is_vip(user_id):
        return False, t("queue.batch_vip_only", lang), 0
    # Atomic claim: two parallel taps must not both pass the check and
    # enqueue two batches of jobs (10.1 — one batch per user).
    if not bot_queue.try_begin_user_batch(user_id):
        return False, t("queue.batch_busy", lang), 0
    try:
        return _start_all_projects_batch_locked(
            bot, user_id=user_id, chat_id=chat_id, lang=lang
        )
    finally:
        bot_queue.end_user_batch(user_id)


def _start_all_projects_batch_locked(
    bot,
    *,
    user_id: int,
    chat_id: int,
    lang: str,
):
    if not user_data_mod.has_user_data(user_id):
        return False, t("data.need", lang), 0

    eligible = batch_eligible_projects(user_id)
    if not eligible:
        return False, t("queue.batch_none", lang), 0

    # Reuse the chain's dashboard when one exists; otherwise post the message
    # the whole batch will live in (never the navigation anchor).
    ahead = bot_queue.user_active_jobs(user_id)
    mid = next((j.message_id for j in ahead if j.message_id), None)
    if mid is None:
        try:
            msg = bot.send_message(
                chat_id, t("queue.batch_starting", lang), parse_mode="HTML"
            )
            mid = msg.message_id
        except Exception:
            mid = None

    batch_id = bot_queue.next_batch_id()
    started = 0
    rows_total = 0
    skipped = 0
    last_job = None
    for project in eligible:
        try:
            rows, _errs, skipped_dup = bot_queue.prepare_job_rows(user_id, project)
        except Exception as exc:  # noqa: BLE001 — one bad module must not kill the batch
            logging.getLogger(__name__).warning(
                "[batch] prepare %s user=%s: %s", project.id, user_id, exc
            )
            skipped += 1
            continue
        if not rows:
            skipped += 1
            continue
        job, _reason, _existing = bot_queue.enqueue_if_allowed(
            user_id=user_id,
            project=project,
            chat_id=chat_id,
            message_id=mid,
            rows=rows,
            skipped_dup=skipped_dup,
            batch_id=batch_id,
            batch_total=len(eligible),
        )
        if job is None:
            skipped += 1
            continue
        started += 1
        rows_total += len(rows)
        last_job = job

    if started == 0 or last_job is None:
        text = t("queue.batch_none", lang)
        if mid is not None:
            try:
                bot.edit_message_text(
                    text, chat_id=chat_id, message_id=mid, parse_mode="HTML"
                )
            except Exception:
                pass
        return False, text, 0

    # Projects that never made it into the queue must not count as «done».
    for job in bot_queue.user_batch_jobs(user_id):
        if job.batch_id == batch_id:
            job.batch_total = started

    # One redraw for the whole batch — not one per enqueued job.
    bot_queue.update_job_message(bot, last_job, lang=lang)
    text = t("queue.batch_started", lang, n=started, rows=rows_total)
    if skipped:
        text += "\n" + t("queue.batch_skipped", lang, n=skipped)
    logging.getLogger(__name__).info(
        "[batch] user=%s batch=%s started=%s rows=%s skipped=%s",
        user_id,
        batch_id,
        started,
        rows_total,
        skipped,
    )
    return True, text, started


_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """Bot HTML -> plain text for the web.

    Every string the mini app shows comes from the bot's i18n, and those are
    written for Telegram's HTML parse mode: `<b>`, `<code>`, escaped entities.
    Handing them to `innerHTML` would render correctly *today* — the
    interpolations all go through `esc()` — but it makes every future i18n key
    a potential injection into a webview that holds the user's Telegram
    session. One forgotten `esc` and the cost is not a broken layout.

    So the tags come off here and the page only ever sets `textContent`. The
    rule the web enforces is "no HTML from the server", which no future edit to
    an unrelated translation file can quietly break.
    """
    out = _TAG_RE.sub("", str(text or ""))
    out = html.unescape(out)
    return out.strip()


def _proxy_check_view(uid: int) -> dict | None:
    """Own-proxy live check as plain data for the mini app (no credentials)."""
    report = bot_proxy_check.last_report(uid)
    if not report:
        return None
    if report.get("status") == "running":
        return {"status": "running", "total": report.get("total", 0)}
    results = report.get("results") or []
    return {
        "status": "done",
        "total": report.get("total", len(results)),
        "alive": sum(1 for r in results if r.get("ok")),
        "results": [
            {
                "label": str(r.get("label") or "proxy"),
                "ok": bool(r.get("ok")),
                "ip": str(r.get("ip") or ""),
                "ms": int(r.get("ms") or 0),
                "error": str(r.get("error") or "")[:60],
            }
            for r in results
        ],
    }


class BotActions(webapp_actions.Actions):
    """The mini app's hands: the same gates the inline buttons go through.

    Every method here is a thin wrapper over a function the keyboard already
    calls. That is deliberate and it is the whole security argument for letting
    a web surface write at all: the web does not get its own path into the
    queue, it gets the existing one. `start_project_queue_job` still checks the
    tier, the project's health, proxies, remaining slots, a duplicate job and
    the global wallet dedup; `enqueue_if_allowed` still re-checks the racy ones
    under its lock. No SQL is issued from here.

    Rollout is staged through `may_write`, because "it works on my account" is
    the only honest way to find out whether it works. Until `WEBAPP_WRITE_USERS`
    says otherwise, only admins can change anything — a default that is safe
    when someone forgets to set it, and not broken for the operator.
    """

    writable = True
    read_only_reason = "запуск из веба пока открыт не всем"

    def __init__(self, bot: telebot.TeleBot) -> None:
        self._bot = bot

    def _notify_admin(self, admin_id: int, text: str) -> None:
        """Same notice the chat path sends, from a caller with no closure."""
        try:
            self._bot.send_message(admin_id, text, parse_mode="HTML",
                                   disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[admin] notify {admin_id} failed: {exc}", flush=True)

    # ---- who may act ------------------------------------------------------

    def may_write(self, user_id: int) -> bool:
        raw = str(getattr(config, "WEBAPP_WRITE_USERS", "") or "").strip()
        if raw == "*":
            return True
        if raw:
            allowed = {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}
            return int(user_id) in allowed
        return int(user_id) in config.ADMIN_IDS

    def gate(self, user_id: int, lang: str = "ru") -> webapp_actions.Denied | None:
        """Ban, then channel membership — the same two gates the chat opens with.

        The web had neither. `is_banned` was checked in nine chat handlers and
        nowhere in the web path, and the channel gate existed only as the
        `subcheck` callback, so `/api/start`, `/api/data` and `/api/buy` were
        reachable by accounts the chat refuses. `webapp/state.py` even sent
        `banned: true` down to the page, where nothing read it.

        Membership comes from `check_subscription_cached`: this runs on every
        poll, four seconds apart, and the uncached call would spend one
        `getChatMember` per poll per open screen on a fact that changes about
        once a week.

        A channel lookup that *errors* — the bot demoted on the channel, a
        Telegram blip — lets the user through. The alternative is locking out
        every user in the world because of a misconfiguration on our side, and
        the chat has always made the same choice: `require_subscription` shows
        the error to the user but the queue's own gates still stand behind it.
        """
        uid = int(user_id)
        try:
            if bot_db.is_banned(uid):
                return webapp_actions.Denied("banned", admin_ux.banned_user_text())
        except Exception as exc:  # noqa: BLE001 — never fail open on a ban
            print(f"[webapp] ban check {uid}: {exc}", flush=True)
            return webapp_actions.Denied("banned", admin_ux.banned_user_text())

        if not config.REQUIRE_CHANNEL:
            return None
        try:
            result, _detail = bot_channel.check_subscription_cached(
                self._bot, uid, lang=lang)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            print(f"[webapp] channel check {uid}: {exc}", flush=True)
            return None
        if result == "not_member":
            return webapp_actions.Denied(
                "not_subscribed",
                _plain(t("gate.text", lang, link=config.REQUIRED_CHANNEL_URL)),
            )
        return None

    def ensure_user(self, user_id: int, username: str = "",
                    first_name: str = "") -> None:
        try:
            bot_db.ensure_user(int(user_id), username or None, first_name or None)
        except Exception as exc:  # noqa: BLE001 — a read must not fail on this
            print(f"[webapp] ensure_user {user_id}: {exc}", flush=True)

    def lang_of(self, user_id: int) -> str:
        try:
            return bot_db.get_lang(int(user_id))
        except Exception:  # noqa: BLE001 — a language lookup must not fail a request
            return "ru"

    # ---- reading ----------------------------------------------------------

    def _reason(self, project: Project, st: dict, job, lang: str) -> tuple[str, str, str]:
        """Why this project cannot start, as (code, short, full).

        ``short`` is one line, because it sits inside a row next to the project
        name. ``full`` is the bot's whole sentence, shown when the user asks —
        "нет данных" is a diagnosis, "загрузи /data в формате handle|0xWallet"
        is the fix, and a row has no space for the second.

        Order matters: it answers the first thing the user would have to fix,
        not every complaint at once. The sentences are the bot's own i18n keys
        so the two surfaces cannot drift into saying different things about the
        same refusal.
        """
        def answer(code: str, text: str) -> tuple[str, str, str]:
            full = _plain(text)
            return code, full.split("\n", 1)[0].strip(), full

        if job is not None:
            return answer("in_queue", t(
                "queue.same_project", lang, id=job.id,
                project=esc(project.name), status=job.status,
            ))
        if st["health_blocked"]:
            key = ("project.health_config" if st["health_status"] == "config"
                   else "project.health_closed")
            code = "config" if st["health_status"] == "config" else "closed"
            return answer(code, t(key, lang))
        if st["left"] <= 0:
            return answer("limit", t("limit.exhausted", lang,
                                     name=esc(project.name), limit=st["lim"], extra=""))
        if not st["has_proxies"]:
            return answer("no_proxies", no_proxies_text(
                lang, name=esc(project.name)))
        if not st["has_data"]:
            return answer("no_data", t("data.need", lang))
        if st["will_send"] <= 0:
            # Distinct from "no data": the list is there, the project has simply
            # seen all of it. That is finished work, not a missing step, and
            # telling the two apart is the difference between "загрузи файл" and
            # "здесь всё сделано".
            if st.get("fresh") == 0 and st["n_data"] > 0:
                return answer("done", t("projects.all_registered", lang,
                                        n=st["n_data"]))
            return answer("nothing_to_send", t("queue.nothing_to_queue", lang))
        return "", "", ""

    def projects(self, user_id: int, lang: str = "ru") -> list[dict]:
        """Every project this user may see, startable or not, with the reason.

        Locked projects — the ones whose `min_ok_limit` is above this tier —
        are included too, marked rather than hidden. Hiding them is what makes
        a paid tier invisible; showing them greyed out with "нужен тариф выше"
        is the same information the bot's locked row carries.
        """
        uid = int(user_id)
        everything = list_projects()
        visible = {p.id for p in bot_limits.visible_projects(everything, uid)}
        # Once for the whole sweep, not once per project: see
        # `_project_start_state`. On a 700-wallet account across 24 projects
        # this was the single biggest cost of a screen refresh.
        data_rows = user_data_mod.load_user_data_rows(uid)
        wallets_lower = {(r.wallet or "").strip().lower() for r in data_rows}
        rows: list[tuple[tuple, dict]] = []
        for project in everything:
            ok_n = bot_db.get_ok_count(uid, project.id)
            if project.id not in visible:
                rows.append((
                    (9, 0, project.name.lower()),
                    {
                        "id": project.id, "name": project.name,
                        "ok_count": ok_n, "limit": 0, "left": 0, "will_send": 0,
                        "can_start": False, "locked": True,
                        "reason_code": "locked",
                        "reason": _plain(t("projects.tier_locked", lang)),
                        "reason_full": _plain(t("projects.tier_locked", lang)),
                        "job_id": None, "job_status": "",
                        "health": "", "self_sourced": False,
                        "can_deliver": False,
                        "link": str(getattr(project, "link", "") or ""),
                        "twitter_url": str(getattr(project, "twitter_url", "") or ""),
                        "mint_url": str(getattr(project, "mint_url", "") or ""),
                        "mint_info": _plain(_project_mint_info(project, lang)),
                    },
                ))
                continue
            st = _project_start_state(project, ok_n, uid, rows=data_rows,
                                      wallet_set=wallets_lower)
            job = bot_queue.user_has_project_job(uid, project.id)
            code, reason, reason_full = self._reason(project, st, job, lang)
            rows.append((
                project_sort_key(project, ok_n, st["health_status"], st.get("fresh")),
                {
                    "id": project.id,
                    "name": project.name,
                    "ok_count": ok_n,
                    "limit": st["lim"],
                    "left": st["left"],
                    "will_send": st["will_send"],
                    "can_start": bool(st["can_start"] and job is None
                                      and st["will_send"] > 0),
                    "locked": False,
                    "reason_code": code,
                    "reason": reason,
                    "reason_full": reason_full,
                    "job_id": job.id if job is not None else None,
                    "job_status": job.status if job is not None else "",
                    "health": st["health_status"],
                    "self_sourced": bool(st["self_sourced"]),
                    "can_deliver": getattr(project, "queue_redeliver", None) is not None,
                    # Links and mint blurb already live in project.toml and the
                    # chat has shown them for months; the web simply never
                    # asked for them. Sent as data, rendered as text — the page
                    # sets textContent and hands URLs to Telegram's own opener.
                    "link": str(getattr(project, "link", "") or ""),
                    "twitter_url": str(getattr(project, "twitter_url", "") or ""),
                    "mint_url": str(getattr(project, "mint_url", "") or ""),
                    "mint_info": _plain(_project_mint_info(project, lang)),
                },
            ))
        rows.sort(key=lambda item: item[0])
        return [row for _key, row in rows]

    def caps(self, user_id: int, lang: str = "ru", projects: list[dict] | None = None) -> dict:
        """Tier, and whether «запустить всё» is real for this user right now.

        Both halves matter. VIP is a permission; having something eligible to
        start is a fact about this minute. Drawing the button on permission
        alone gives the user a tap that answers "нечего запускать", which reads
        as a broken screen rather than an empty one.

        The count comes from the rows the caller already built when it can.
        `batch_eligible_projects` answers the same question by recomputing
        `_project_start_state` for every project — 200 ms on a 21-project
        account, which the screen was paying twice every four seconds.
        """
        uid = int(user_id)
        tier = bot_limits.limit_mode(uid)
        if not bot_limits.is_vip(uid):
            return {"tier": tier, "can_start_all": False,
                    "start_all_reason": _plain(t("queue.batch_vip_only", lang))}
        if bot_queue.user_batch_active(uid):
            return {"tier": tier, "can_start_all": False,
                    "start_all_reason": _plain(t("queue.batch_busy", lang))}
        if projects is None:
            eligible = len(batch_eligible_projects(uid))
        else:
            eligible = sum(1 for row in projects if row.get("can_start"))
        return {
            "tier": tier,
            "can_start_all": eligible > 0,
            "eligible": eligible,
            "start_all_reason": "" if eligible else _plain(t("queue.batch_none", lang)),
        }

    def data(self, user_id: int, lang: str = "ru") -> dict:
        """The uploaded accounts, summarised — never the accounts themselves.

        The screen needs to answer "есть ли данные и сколько", and that is all
        it gets. Sending 700 wallet addresses down to a webview on every poll
        would put the operator's whole list into a browser cache, a screenshot
        and anyone's shoulder-surf, to render a number.
        """
        uid = int(user_id)
        return {
            "count": user_data_mod.count_user_data(uid),
            "max_lines": int(getattr(config, "USER_DATA_MAX_LINES", 1000) or 1000),
            "max_bytes": int(getattr(config, "USER_DATA_MAX_BYTES", 512 * 1024)
                             or 512 * 1024),
        }

    def proxies(self, user_id: int, lang: str = "ru") -> dict:
        """Where submits would go out from, and whether this tier may change it."""
        uid = int(user_id)
        source, count = _proxy_readiness(uid)
        health = bot_db.user_proxy_health(uid)
        return {
            "count": count,
            "source": source,
            "shared_enabled": bool(shared_enabled()),
            "may_upload": bool(bot_limits.may_upload_proxies(uid)),
            "own_file": bool(user_proxies_mod.own_proxy_file_exists(uid)),
            "max_lines": int(getattr(config, "USER_PROXY_MAX_LINES", 1000) or 1000),
            "max_bytes": int(getattr(config, "USER_PROXY_MAX_BYTES", 512 * 1024)
                             or 512 * 1024),
            "health": {
                "rows": health.get("rows", 0),
                "fails": health.get("fails", 0),
                "fails_proxy": health.get("fails_proxy", 0),
            },
            # Live-check state for the Моё tab: running, or the last report
            # (labels carry host:port only — credentials never leave the file).
            "check": _proxy_check_view(uid),
        }

    def privatka(self, user_id: int, lang: str = "ru") -> dict:
        """Always-RU upsell payload for the mini app (plain text)."""
        from bot import privatka as priv

        del lang
        uid = int(user_id)
        win = priv.enabled() and priv.is_free_win(priv.last_done_job(uid))
        return {
            "enabled": priv.enabled(),
            "url": priv.pay_url() if priv.enabled() else "",
            "win_banner": priv.win_line() if win else "",
            "basic_line": priv.basic_until_line(uid) if priv.enabled() else "",
            "branch": priv.branch(uid) if priv.enabled() else "off",
        }

    def notice(self, user_id: int, lang: str = "ru") -> dict:
        """Ops banner for the mini app (plain text — the page uses textContent)."""
        del user_id
        text = own_proxy_only_notice(lang)
        if not text:
            return {"code": "", "text": ""}
        return {"code": "own_proxy_only", "text": _plain(text)}

    def billing(self, user_id: int, lang: str = "ru") -> dict:
        """The price list, what is active, and any payment still open.

        `payments_available` is the subsystem switch and it is checked here
        rather than in the page: a screen that draws a shop the server will
        refuse is a screen that lies. When it is off the whole block is absent
        and the UI has nothing to render.
        """
        uid = int(user_id)
        if not billing_ux.payments_available(uid):
            return {"enabled": False, "plans": [], "active": None,
                    "open_payment": None, "sales_open": False}
        current = billing.active_plan(uid)
        open_payment = bot_db.get_open_payment_for_user(uid)
        return {
            "enabled": True,
            "sales_open": False,
            "tier": bot_limits.limit_mode(uid),
            "active": ({"plan": current[0], "until": current[1],
                        "days_left": billing.days_left(uid)} if current else None),
            "plans": [
                {
                    "id": p.id,
                    "emoji": p.emoji,
                    "price_usd": str(p.price_usd),
                    "days": p.days,
                    "ok_limit": p.ok_limit,
                    "parallel_jobs": p.parallel_jobs,
                    "purchasable": bool(billing.is_purchasable(p.id)),
                }
                for p in billing.plan_catalog()
                if p.id != billing.PLAN_FREE
            ],
            "open_payment": self._payment_view(open_payment),
        }

    @staticmethod
    def _payment_view(record) -> dict | None:
        """The parts of a payment the user has to act on, and nothing else.

        The address and the exact amount go out in full and unformatted: this
        is a string someone will copy into a wallet, and a prettified or
        truncated one sends money nowhere.
        """
        if record is None:
            return None
        return {
            "payment_id": str(record["payment_id"]),
            "plan": str(record["plan"] or ""),
            "status": str(record["status"] or "waiting"),
            "pay_address": str(record["pay_address"] or ""),
            "pay_amount": str(record["pay_amount"] or ""),
            "pay_currency": str(record["pay_currency"] or ""),
            "price_amount": str(record["price_amount"] or ""),
            "valid_until": str(record["valid_until"] or ""),
            "credited": bool(record["credited_at"]),
        }

    def report(self, user_id: int, lang: str = "ru") -> dict:
        """The last bulk run, whole. No paging — a page has room to scroll."""
        raw = bulk_report_load(int(user_id)) or {}
        errors = [_plain(str(e)) for e in (raw.get("errors") or [])]
        stats = raw.get("stats") or {}
        return {
            "has": bool(raw),
            "project": str(raw.get("project") or ""),
            "errors": errors[:500],
            "errors_total": len(errors),
            "stats": {k: int(v or 0) for k, v in stats.items() if isinstance(v, (int, float))},
        }

    def stats(self, user_id: int, days: int = 7, lang: str = "ru") -> dict:
        del lang
        raw = bot_db.user_stats_daily(int(user_id), days=int(days))
        names = {p.id: p.name for p in list_projects()}
        return {
            "days": raw["days"],
            "totals": raw["totals"],
            "by_day": raw["by_day"],
            "by_project": [
                {
                    "project_id": p["project_id"],
                    "name": names.get(p["project_id"], p["project_id"]),
                    "ok": p["ok"],
                    "already": p["already"],
                }
                for p in raw["by_project"][:10]
            ],
        }

    def mints(self, user_id: int, lang: str = "ru") -> dict:
        """The calendar, plus the hand-written blurbs as a fallback.

        Seventeen projects carry a `mint_info` sentence in project.toml that a
        human wrote by hand. It has no structure — no parseable date, no
        separable price — so it cannot drive a calendar, but it is real
        information and hiding it would make an empty screen out of a full
        one. Structured rows lead; a project with only prose appears below,
        marked as prose.
        """
        if not config.MINTS_ENABLED:
            return {"rows": [], "may_edit": False, "enabled": False}
        uid = int(user_id)
        projects = {p.id: p for p in list_projects()}
        names = {pid: p.name for pid, p in projects.items()}
        rows = bot_mints.calendar(names)
        for row in rows:
            # Links belong on the calendar, not only on the project card: the
            # moment someone reads "public 18:00" is the moment they want the
            # page they will mint on.
            project = projects.get(row["project_id"])
            # `link` is the apply form the bot submits to; project.toml's own
            # `mint_url` turned out to hold the home page for every project
            # that sets it, so it is shown as the site and never as the mint.
            # The mint button comes only from the calendar's own field, which
            # means no button until somebody sets a real one.
            row["apply_url"] = str(getattr(project, "link", "") or "") if project else ""
            row["site_url"] = str(getattr(project, "mint_url", "") or "") if project else ""
            row["twitter_url"] = str(getattr(project, "twitter_url", "") or "") if project else ""
            row["mint_url"] = row.get("mint_url") or ""
        have = {r["project_id"] for r in rows}
        for project in list_projects():
            if project.id in have:
                continue
            blurb = _plain(_project_mint_info(project, lang))
            if not blurb:
                continue
            rows.append({
                "project_id": project.id, "project": project.name, "live": True,
                "supply": None, "chain": "", "note": blurb, "source_url": "",
                "source": "toml", "updated_at": "", "phases": [],
                "next_at": None, "last_at": None,
                "apply_url": str(getattr(project, "link", "") or ""),
                "site_url": str(getattr(project, "mint_url", "") or ""),
                "twitter_url": str(getattr(project, "twitter_url", "") or ""),
                "mint_url": "",
            })
        return {
            "rows": rows,
            # Editing publishes to everyone, so it is an admin capability, not
            # the per-user write gate.
            "may_edit": uid in config.ADMIN_IDS,
            "phases": list(bot_mints.PHASES),
            # The tab's own switch: the page draws «Минты» only while this is
            # true, and the disabled branch above answers the same key False.
            "enabled": True,
        }

    # ---- writing ----------------------------------------------------------

    def _require_admin(self, user_id: int, lang: str) -> None:
        if int(user_id) not in config.ADMIN_IDS:
            raise webapp_actions.Denied(
                "not_admin", "Календарь виден всем — менять его может только админ.")

    def save_mint(self, user_id: int, payload: dict, lang: str = "ru") -> dict:
        """Write one project's mint facts. Admin only, and validated here.

        A date that will not parse is refused rather than stored as TBA: the
        difference between "no date announced" and "the date you typed was
        unreadable" is the difference between a calendar that is honest and one
        that quietly loses what you entered.
        """
        self._require_admin(user_id, lang)
        pid = str(payload.get("project_id") or "").strip()
        project = get_project(pid) if pid else None
        known = next((r for r in bot_mints.calendar({}) if r["project_id"] == pid), None)
        # A calendar-only drop — Hood Citizens mints on OpenSea with no form —
        # needs a name once and no registry entry after that. The chat command
        # has accepted this since it was added; the web path had not, and
        # refused everything the registry did not know.
        display = (project.name if project
                   else str(payload.get("name") or "") or (known or {}).get("project", ""))
        if not pid or (project is None and not display):
            raise webapp_actions.Denied("unknown_project",
                                        _plain(t("toast.project_unavailable", lang)))
        supply_raw = payload.get("supply")
        try:
            supply = int(supply_raw) if str(supply_raw or "").strip() else None
        except (TypeError, ValueError):
            raise webapp_actions.Denied("bad_request", "саплай — это число") from None
        if supply is not None and not (0 < supply < 100_000_000):
            raise webapp_actions.Denied("bad_request", "саплай вне разумных границ")

        phases_in = payload.get("phases")
        if not isinstance(phases_in, list):
            phases_in = []
        parsed: list[tuple[str, str | None, str, str, str]] = []
        for item in phases_in[:6]:
            if not isinstance(item, dict):
                continue
            kind = bot_mints.normalize_phase(str(item.get("kind") or ""))
            raw_when = str(item.get("starts_at") or "").strip()
            when = bot_mints.parse_when(raw_when)
            if raw_when and when is None and raw_when.lower() not in ("tba", "-", "—"):
                raise webapp_actions.Denied(
                    "bad_date",
                    f"Не понял дату «{raw_when[:40]}». Формат: 2026-08-22 18:00 (UTC).")
            # A window's own name travels beside the key — but only when it
            # differs from the key. Storing "ratlist" as the label of `ratlist`
            # made the screen print it lowercase; left empty, `phase_label`
            # renders RATLIST from the slug, which is what a project writes.
            label = str(item.get("label") or "")[:32]
            if label.strip().lower() == kind:
                label = ""
            parsed.append((kind, when, str(item.get("price") or ""),
                           str(item.get("note") or ""), label))

        bot_mints.set_mint(
            pid, supply=supply, chain=str(payload.get("chain") or ""),
            note=str(payload.get("note") or ""),
            source_url=str(payload.get("source_url") or ""),
            # These two were simply not passed. The field existed, the storage
            # accepted it, and the web editor dropped the mint link and the
            # display name on the floor without a word.
            mint_url=str(payload.get("mint_url") or ""),
            name=display,
            source="manual", updated_by=int(user_id),
        )
        for kind, when, price, note, label in parsed:
            bot_mints.set_phase(pid, kind, starts_at=when, price=price,
                                note=note, label=label)
        return {"ok": True, "message": "Сохранено."}

    def clear_mint(self, user_id: int, project_id: str, lang: str = "ru") -> dict:
        self._require_admin(user_id, lang)
        bot_mints.clear_project(str(project_id or "").strip())
        return {"ok": True, "message": "Удалено из календаря."}

    def set_lang(self, user_id: int, lang_code: str) -> dict:
        code = str(lang_code or "").strip().lower()
        if code not in ("ru", "en"):
            raise webapp_actions.Denied("bad_request", "язык не поддерживается")
        bot_db.set_lang(int(user_id), code)
        return {"ok": True, "lang": code,
                "message": _plain(t("lang.set_ru" if code == "ru" else "lang.set_en", code))}

    def set_captcha_key(
        self, user_id: int, provider: str, key: str, lang: str = "ru"
    ) -> dict:
        """Store the user's own captcha solver key (validated live first).

        Same path as the chat's /keys upload: balance check, 0600 file, tier
        cache invalidated, and the key is never echoed back — only a mask.
        """
        uid = int(user_id)
        allowed, why = bot_rate.allow(uid, "key_upload", lang=lang)
        if not allowed:
            raise webapp_actions.Denied("rate", _plain(why))
        ok, balance, detail = user_captcha_mod.validate_key(provider, key)
        if not ok:
            raise webapp_actions.Denied(
                "rejected",
                _plain(
                    t(
                        "keys.network_down" if "unreachable" in detail else "keys.invalid",
                        lang,
                        reason=detail,
                    )
                ),
            )
        user_captcha_mod.save_key(uid, provider, key)
        bot_limits.invalidate(uid)
        label = {"capsolver": "Capsolver", "2captcha": "2Captcha",
                 "anticaptcha": "Anti-Captcha"}[user_captcha_mod.normalize_provider(provider)]
        return {
            "ok": True,
            "masked": user_captcha_mod.masked(uid),
            "balance": f"{balance:g}",
            "message": _plain(
                t("keys.saved", lang, provider=label, balance=f"{balance:g}")
            ),
        }

    def clear_captcha_key(self, user_id: int, lang: str = "ru") -> dict:
        uid = int(user_id)
        user_captcha_mod.clear_key(uid)
        bot_limits.invalidate(uid)
        return {"ok": True, "message": _plain(t("keys.cleared", lang))}

    def set_auto_reg(self, user_id: int, on: bool, lang: str = "ru") -> dict:
        uid = int(user_id)
        bot_db.set_auto_reg(uid, bool(on))
        if on and not user_data_mod.has_user_data(uid):
            return {
                "ok": True,
                "auto_reg": True,
                "message": _plain(t("autoreg.need_data_turn_on", lang)),
            }
        return {
            "ok": True,
            "auto_reg": bool(on),
            "message": _plain(
                t("autoreg.toggled_on" if on else "autoreg.toggled_off", lang)
            ),
        }

    def user_setup(self, user_id: int) -> dict:
        """Captcha key mask + auto-registration flag (no secrets, no network)."""
        uid = int(user_id)
        return {
            "captcha_key": user_captcha_mod.masked(uid),
            "auto_reg": bot_db.get_auto_reg(uid),
            "mint_remind": bot_db.get_mint_remind(uid),
            "mints_enabled": bool(config.MINTS_ENABLED),
        }

    def set_mint_remind(self, user_id: int, on: bool, lang: str = "ru") -> dict:
        uid = int(user_id)
        bot_db.set_mint_remind(uid, bool(on))
        return {"ok": True, "mint_remind": bool(on)}

    def buy(self, user_id: int, plan_id: str, lang: str = "ru") -> dict:
        """New Basic invoices are refused — money goes through the external upsell bot.

        An already-open address is still returned so a transfer in flight
        can settle.
        """
        del plan_id
        uid = int(user_id)
        existing = bot_db.get_open_payment_for_user(uid)
        if existing is None:
            raise webapp_actions.Denied(
                "privatka", _plain(t("priv.btn_pay", "ru", bot=config.UPSELL_BOT))
            )
        return {
            "ok": True,
            "reused": True,
            "payment": self._payment_view(existing),
            "message": _plain(t("pay.open_exists", lang)),
        }

    def check_payment(self, user_id: int, payment_id: str, lang: str = "ru") -> dict:
        uid = int(user_id)
        record = bot_db.get_payment_record(str(payment_id or "").strip())
        if record is None or int(record["user_id"]) != uid:
            # Same answer for "no such payment" and "someone else's": telling
            # them apart would let a caller enumerate other people's invoices.
            raise webapp_actions.Denied("unknown", _plain(t("pay.not_yours", lang)))
        allowed, why = bot_rate.allow(uid, "pay_check", lang=lang)
        if not allowed:
            raise webapp_actions.Denied("rate", _plain(why))
        try:
            fresh = payments_np.get_payment(record["payment_id"])
        except payments_np.NowPaymentsError as exc:
            raise webapp_actions.Denied(
                "provider", _plain(bot_errors.user_message("FAIL", str(exc), lang=lang))
            ) from None
        # The same settlement path the chat button and the background poller
        # use. Crediting is decided by the provider's status, never by who
        # asked — which is why exposing this button costs nothing.
        outcome = payments_poll.handle_status(
            record, fresh, bot=self._bot, send=self._notify_admin)
        credited = outcome in ("credited", "already_credited")
        return {
            "ok": True,
            "credited": credited,
            "message": _plain(t("pay.status_done" if credited else "pay.not_yet", lang)),
            "payment": self._payment_view(bot_db.get_payment_record(record["payment_id"])),
        }

    def cancel_payment(self, user_id: int, payment_id: str, lang: str = "ru") -> dict:
        uid = int(user_id)
        record = bot_db.get_payment_record(str(payment_id or "").strip())
        if record is None or int(record["user_id"]) != uid:
            # Same answer for "no such payment" and "someone else's": telling
            # them apart would let a caller enumerate other people's invoices.
            raise webapp_actions.Denied("unknown", _plain(t("pay.not_yours", lang)))
        if not record["credited_at"]:
            # Local only, exactly as in the chat: NOWPayments keeps the address
            # alive and a late transfer is still credited by the poller. So
            # this hides the card, it does not throw money away.
            bot_db.update_payment_status(record["payment_id"], "expired", mark_polled=False)
        return {"ok": True, "message": _plain(t("pay.cancelled", lang))}

    def deliver(self, user_id: int, project_id: str, lang: str = "ru") -> dict:
        """Re-send a project's artifact — into the chat, not into the browser.

        The artifact is a key file. It goes where the bot already sends it and
        where Telegram's own storage rules apply, not down an HTTP response
        into a webview cache.
        """
        uid = int(user_id)
        project = get_project(str(project_id or "").strip())
        hook = getattr(project, "queue_redeliver", None) if project else None
        if project is None or hook is None or not bot_limits.project_allowed(project, uid):
            raise webapp_actions.Denied("unknown", _plain(t("toast.project_unavailable", lang)))
        if bot_queue.user_has_project_job(uid, project.id) is not None:
            # Handing the keys over erases the bot's copy, and a running job
            # still needs them for rows it has not reached. 226 rows died that
            # way once; the guard is not optional on a second surface.
            raise webapp_actions.Denied("busy", _plain(t("toast.keys_busy", lang)))
        try:
            sent = int(hook(user_id=uid, bot=self._bot) or 0)
        except Exception as exc:  # noqa: BLE001 — report, never break the screen
            print(f"[deliver] web {project.id} user={uid}: {exc}", flush=True)
            sent = 0
        return {"ok": True, "sent": sent,
                "message": _plain(t("toast.keys_sent" if sent else "toast.keys_none",
                                    lang, n=sent))}

    # ---- gform constructor (user Google Forms, mini app) -------------------

    def gforms(self, user_id: int, lang: str = "ru") -> list[dict]:
        from bot import gform as bot_gform

        return bot_gform.user_forms(int(user_id))

    def gform_parse(self, user_id: int, url: str, lang: str = "ru") -> dict:
        from bot import gform as bot_gform

        uid = int(user_id)
        link = str(url or "").strip()
        left = bot_gform.check_parse_cooldown(uid)
        if left > 0:
            raise webapp_actions.Denied(
                "cooldown",
                f"Не чаще раза в {int(bot_gform.PARSE_COOLDOWN_S)} сек — "
                f"подожди {int(left) + 1} сек.")
        try:
            html = bot_gform.fetch_viewform(link)
            form = bot_gform.parse_form(link, html)
        except bot_gform.GformError as exc:
            raise webapp_actions.Denied("refused", _plain(str(exc))) from None
        bot_gform.cache_form(uid, form)
        fields = [{
            "entry": f["entry"], "title": f["title"],
            "options": f["options"], "required": f["required"],
            "suggest": bot_gform.suggest_source(f),
        } for f in form["fields"]]
        return {"ok": True, "form": {"title": form["title"], "url": form["url"],
                                     "fields": fields}}

    def gform_create(self, user_id: int, mapping, lang: str = "ru",
                      *, name: str = "", link: str = "") -> dict:
        from bot import gform as bot_gform
        from bot.registry import reload_projects

        uid = int(user_id)
        form = bot_gform.pop_form(uid)
        if form is None:
            raise webapp_actions.Denied(
                "stale", "Форма устарела — распарси ссылку заново.")
        try:
            pid, _folder = bot_gform.build_project(
                form, dict(mapping or {}), uid, name=name, link=link)
        except bot_gform.GformError as exc:
            # Put the form back: a refused mapping is fixable without paying
            # another parse (and the cooldown that guards it).
            bot_gform.cache_form(uid, form)
            raise webapp_actions.Denied("refused", _plain(str(exc))) from None
        reload_projects()
        self._gform_admin_alert(uid, pid)
        return {
            "ok": True, "project_id": pid,
            "message": _plain(
                "Проект создан и виден только тебе. Лимит — по твоему тарифу. "
                "Админ решает, показывать ли форму всем."),
        }

    def gform_delete(self, user_id: int, project_id: str, lang: str = "ru") -> dict:
        from bot import gform as bot_gform
        from bot.registry import reload_projects
        from bot import submit_queue

        uid = int(user_id)
        pid = str(project_id or "").strip()
        try:
            bot_gform.delete_form(
                uid, pid, cancel_jobs=submit_queue.cancel_jobs_for_project)
        except bot_gform.GformError as exc:
            raise webapp_actions.Denied("refused", _plain(str(exc))) from None
        reload_projects()
        return {"ok": True, "message": _plain("Форма удалена.")}

    def _gform_admin_alert(self, uid: int, pid: str) -> None:
        """One DM per admin: publish this user's form for everyone, or not.

        The project is live for its creator either way (operator, 2026-09-10);
        the buttons only decide whether it leaves `only_user_ids`.
        """
        from telebot import types as _types

        from bot import gform as bot_gform

        d = bot_gform.load_def(pid) or {}
        user = bot_db.get_user_row(uid) or {}
        who = ("@" + str(user.get("username"))
               if user.get("username") else (user.get("first_name") or str(uid)))
        rows = []
        mapping = d.get("mapping") or {}
        for f in (d.get("fields") or [])[:10]:
            src = mapping.get(f.get("entry"))
            if src and src != "skip":
                mark = "*" if f.get("required") else " "
                rows.append(f"{mark} {f.get('entry')} = {src}")
        body = (
            f"🛠 <b>Форма на публикацию</b>\n"
            f"От: {esc(who)} (id {uid})\n"
            f"Форма: {esc(d.get('title') or pid)}\n"
            f"{esc(d.get('url') or '')}\n\n"
            + "\n".join(rows)
            + ("\n…" if len(d.get("fields") or []) > 10 else "")
            + "\n\nПоказать всем в списке проектов?"
        )
        kb = _types.InlineKeyboardMarkup()
        kb.row(
            _types.InlineKeyboardButton("✅ Показать всем",
                                        callback_data=f"gform:pub:{pid}"),
            _types.InlineKeyboardButton("✖️ Только ему",
                                        callback_data=f"gform:no:{pid}"),
        )
        for aid in config.ADMIN_IDS:
            try:
                self._bot.send_message(aid, body, parse_mode="HTML",
                                       disable_web_page_preview=True,
                                       reply_markup=kb)
            except Exception as exc:  # noqa: BLE001 — one dead admin chat ≠ stop
                print(f"[gform] admin alert {aid} failed: {exc}", flush=True)

    def suggest(self, user_id: int, text: str, lang: str = "ru") -> dict:
        uid = int(user_id)
        body = str(text or "").strip()
        if len(body) < SUGGEST_MIN_LEN:
            raise webapp_actions.Denied(
                "too_short", f"Слишком коротко — минимум {SUGGEST_MIN_LEN} символов.")
        if len(body) > SUGGEST_MAX_LEN:
            raise webapp_actions.Denied(
                "too_long", f"Слишком длинно — максимум {SUGGEST_MAX_LEN} символов.")
        # `get_user_row`, not `get_user` — the latter does not exist, and this
        # raised AttributeError for every suggestion sent from the web until a
        # user hit it. The name was never checked against bot/db.py.
        user = bot_db.get_user_row(uid) or {}
        result = bot_db.add_suggestion(
            uid, user.get("username") or "", user.get("first_name") or "", body)
        if not result.get("ok"):
            if result.get("reason") == "unknown_user":
                # Should be unreachable now that every request calls
                # `ensure_user`, but the two used to share one message and the
                # wrong one sent a first-time visitor away for a day.
                raise webapp_actions.Denied(
                    "unknown_user", "Напишите боту /start — он вас ещё не знает.")
            raise webapp_actions.Denied(
                "daily_limit", "Одно предложение в сутки (UTC) — сегодня уже было.")
        # The chat handler DMs the admins; this path did not, so a suggestion
        # sent from the app was saved, answered "отправлено", and reached
        # nobody. A stored row an operator never sees is the same as a lost one.
        sent = notify_admins_suggestion(
            self._bot, user_id=uid,
            username=user.get("username"), first_name=user.get("first_name"),
            body=body, suggestion_id=int(result.get("id") or 0),
            utc_day=str(result.get("utc_day") or ""),
        )
        return {"ok": True, "notified": sent, "message": "Предложение отправлено."}

    def save_data(self, user_id: int, text: str, lang: str = "ru") -> dict:
        """Replace this user's accounts file with what the web sent.

        Same validator as the uploaded .txt in the chat — one parser, so a line
        the bot rejects is not silently accepted here. Replace, not append: the
        bot's /data has always worked that way, and two different meanings for
        the same word across two surfaces is how someone loses a list.
        """
        uid = int(user_id)
        # The same hourly ceiling the chat upload has. The web's own budget is
        # per-minute and says nothing about an hour, so without this the same
        # user could replace the file forty times an hour here and five times
        # there — one gate per surface is how two surfaces drift apart.
        allowed, why = bot_rate.allow(uid, "bulk_file", lang=lang)
        if not allowed:
            raise webapp_actions.Denied("rate", _plain(why))
        ok, message, count = user_data_mod.save_user_data_from_bytes(
            uid, text.encode("utf-8"), lang=lang
        )
        if not ok:
            raise webapp_actions.Denied("rejected", _plain(message))
        return {"ok": True, "message": _plain(message), "count": count}

    def clear_data(self, user_id: int, lang: str = "ru") -> dict:
        removed = user_data_mod.clear_user_data(int(user_id))
        return {"ok": True, "removed": bool(removed),
                "message": _plain(t("data.deleted" if removed else "data.empty", lang))}

    def delete_data_row(self, user_id: int, row: int, lang: str = "ru") -> dict:
        # By number, not by contents: the mini app never receives the wallet
        # list itself, so the row the user picked in the chat editor and the
        # number typed here address the same file through the same parser.
        uid = int(user_id)
        ok = user_data_mod.delete_user_data_row(uid, int(row))
        left = user_data_mod.count_user_data(uid)
        return {
            "ok": ok,
            "count": left,
            "message": _plain(
                t("toast.data_row_deleted", lang, n=int(row), n_left=left)
                if ok
                else t("toast.data_bad_row", lang)
            ),
        }

    def export_results(self, user_id: int, project_id: str, lang: str = "ru") -> dict:
        pid = str(project_id or "").strip()
        rows = bot_db.user_project_registrations(int(user_id), pid)
        project = get_project(pid) if pid else None
        name = project.name if project is not None else pid
        if not rows:
            return {"ok": False,
                    "message": _plain(t("toast.no_results", lang, name=name))}
        lines = [f"# {name} — registrations ({len(rows)})",
                 "# handle\twallet\tstatus\tregistered_at"]
        for r in rows:
            lines.append("\t".join((
                str(r.get("handle") or ""),
                str(r.get("wallet") or ""),
                str(r.get("status") or ""),
                str(r.get("created_at") or "")[:19],
            )))
        return {"ok": True, "name": name, "count": len(rows),
                "text": "\n".join(lines) + "\n"}

    def save_proxies(self, user_id: int, text: str, lang: str = "ru") -> dict:
        uid = int(user_id)
        if not bot_limits.may_upload_proxies(uid):
            raise webapp_actions.Denied("tier", _plain(t("proxy.upload_tier", lang)))
        allowed, why = bot_rate.allow(uid, "proxy_file", lang=lang)
        if not allowed:
            raise webapp_actions.Denied("rate", _plain(why))
        ok, message, count = user_proxies_mod.save_user_proxies_from_bytes(
            uid, text.encode("utf-8"), lang=lang
        )
        if not ok:
            raise webapp_actions.Denied("rejected", _plain(message))
        return {"ok": True, "message": _plain(message), "count": count}

    def clear_proxies(self, user_id: int, lang: str = "ru") -> dict:
        # Deleting own proxies drops the tier back down, which is a real
        # consequence and not one to discover afterwards. The page confirms
        # first; this is the second half of that agreement.
        removed = user_proxies_mod.clear_user_proxies(int(user_id))
        if removed:
            msg_key = "proxy.removed"
        elif not shared_enabled():
            msg_key = "proxy.none_own_off"
        else:
            msg_key = "proxy.none_own"
        return {"ok": True, "removed": bool(removed),
                "message": _plain(t(msg_key, lang))}

    def check_proxies(self, user_id: int, lang: str = "ru") -> dict:
        """Start the live own-proxy check (bot/proxy_check.py); the page
        reads the report back from the state poll — a synchronous answer
        would hold the HTTP request for up to a minute of proxy timeouts."""
        uid = int(user_id)
        urls = user_proxies_mod.load_user_proxy_urls(uid)
        if not urls:
            raise webapp_actions.Denied("no_proxies", _plain(t("proxy.check_none", lang)))
        allowed, wait = bot_proxy_check.may_run(uid)
        if not allowed:
            raise webapp_actions.Denied(
                "cooldown", _plain(t("proxy.check_cooldown", lang, sec=wait)))
        bot_proxy_check.start(uid, urls)
        return {"ok": True, "status": "started"}

    def start(self, user_id: int, project_id: str, lang: str = "ru") -> dict:
        project = get_project(project_id)
        if project is None:
            raise webapp_actions.Denied(
                "unknown_project", _plain(t("toast.project_unavailable", lang)))
        # chat_id == user_id: a mini app is launched from the private chat with
        # the bot, and that is where the progress dashboard belongs.
        ok, message, job = start_project_queue_job(
            self._bot, user_id=int(user_id), chat_id=int(user_id),
            project=project, lang=lang,
        )
        if not ok:
            raise webapp_actions.Denied("refused", _plain(message))
        # No message on success at all: act() turns any message into a native
        # Telegram alert, and after a start tap even one line is noise. The
        # row itself flips to «идёт» and the «Очередь» badge ticks — that is
        # the confirmation. Refusals keep their full sentences.
        return {"ok": True, "job_id": getattr(job, "id", None)}

    def start_all(self, user_id: int, lang: str = "ru") -> dict:
        ok, message, started = start_all_projects_batch(
            self._bot, user_id=int(user_id), chat_id=int(user_id), lang=lang,
        )
        if not ok:
            raise webapp_actions.Denied("refused", _plain(message))
        return {"ok": True, "message": _plain(message), "started": started}

    def cancel(self, user_id: int, job_id: int, lang: str = "ru") -> dict:
        ok, reason, job = bot_queue.cancel_user_job(int(user_id), job_id=int(job_id))
        if not ok or job is None:
            raise webapp_actions.Denied("refused", _plain(reason))
        try:
            bot_queue.update_job_message(self._bot, job, lang=lang)
        except Exception:  # noqa: BLE001 — the cancel already happened
            pass
        # A queued job is gone the moment it is pulled from the queue; a
        # running one stops after the row in flight. Saying "остановлено" for
        # the second would be a lie the user catches within seconds, watching
        # the counter tick once more.
        key = "queue.cancelled" if job.status == "cancelled" else "queue.cancelling"
        return {"ok": True, "message": _plain(t(key, lang, id=job.id))}




def notify_mint_proposal(bot, proposal: dict) -> int:
    """DM every admin one finding, with its quote and a link to the post."""
    if not config.ADMIN_IDS:
        return 0
    project = get_project(proposal["project_id"])
    name = project.name if project else proposal["project_id"]
    current = next(
        (row for row in bot_mints.calendar({})
         if row["project_id"] == proposal["project_id"]), None)
    text = mint_ux.proposal_text(proposal, name, current)
    sent = 0
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, parse_mode="HTML",
                             disable_web_page_preview=True,
                             reply_markup=_mint_keyboard(proposal["id"]))
            sent += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[mint] notify {admin_id}: {exc}", flush=True)
    return sent


def run_mint_scan(bot) -> dict:
    """One pass over every project with a handle, then DM what survived.

    Notifying inside the loop rather than after it: a scan that dies on project
    twelve should still have delivered the first eleven findings.
    """
    load_projects()
    result = bot_mint_scan.scan_all(list_projects())
    for proposal_id in result.get("proposals", []):
        row = bot_mints.get_proposal(proposal_id)
        if row:
            notify_mint_proposal(bot, row)
    if result.get("errors"):
        print(f"[mint] scan errors: {result['errors'][:5]}", flush=True)
    unread = result.get("unread") or []
    print(f"[mint] scanned {result.get('projects')} projects, "
          f"{len(result.get('proposals', []))} proposals"
          + (f", {len(unread)} UNREAD: {unread}" if unread else ""), flush=True)
    return result


def start_mint_scanner(bot) -> None:
    """Background scan on a fixed interval, skipped entirely without a token.

    The interval is nearly free to shorten: every read passes `since_id`, so a
    check that finds nothing new costs nothing at X. Six hours is the operator's
    choice, not a cost ceiling.
    """
    if not config.MINTS_ENABLED:
        print("[mint] scanner off (MINTS_ENABLED=0)", flush=True)
        return
    if not bot_x_client.enabled():
        print("[mint] X_BEARER_TOKEN not set — scanner off", flush=True)
        return
    hours = float(os.getenv("MINT_SCAN_HOURS", "6") or 6)
    if hours <= 0:
        print("[mint] scanner disabled by MINT_SCAN_HOURS=0", flush=True)
        return

    def _loop() -> None:
        # A first pass is not run at boot: a restart during an announcement
        # would DM the same finding again, and the schedule catches it anyway.
        while True:
            time.sleep(max(600.0, hours * 3600.0))
            try:
                run_mint_scan(bot)
            except Exception as exc:  # noqa: BLE001 — a scan must not kill its thread
                print(f"[mint] scan failed: {type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=_loop, name="mint-scanner", daemon=True).start()
    print(f"[mint] scanner every {hours}h", flush=True)


def set_bot_commands(bot: telebot.TeleBot) -> None:
    """Slash-command menu. Does not touch the blue Mini App menu button."""
    try:
        bot.set_my_commands(
            [
                types.BotCommand("start", "Проекты"),
                types.BotCommand("priv", "⭐ Приватка"),
                types.BotCommand("data", "Аккаунты"),
                types.BotCommand("queue", "Очередь"),
                types.BotCommand("proxy", "Прокси"),
                types.BotCommand("help", "Помощь"),
            ]
        )
    except Exception as exc:  # noqa: BLE001 — a menu list must not stop a boot
        print(f"[bot] set_my_commands: {type(exc).__name__}: {exc}", flush=True)


def set_menu_button(bot: telebot.TeleBot) -> bool:
    """Put the app on the chat's menu button, the one beside the paperclip.

    Set from here rather than clicked once in BotFather so it survives a token
    change and cannot drift from `WEBAPP_URL`. Clearing `WEBAPP_URL` puts the
    default commands button back, so turning the app off is still one variable.

    This is not the Main Mini App — that one lives on the bot's profile and can
    only be configured in BotFather; the Bot API has no method for it.
    """
    try:
        if config.WEBAPP_URL:
            bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp(
                type="web_app", text="Приложение",
                web_app=types.WebAppInfo(url=config.WEBAPP_URL)))
            print(f"[webapp] menu button -> {config.WEBAPP_URL}", flush=True)
        else:
            bot.set_chat_menu_button(menu_button=types.MenuButtonCommands(type="commands"))
            print("[webapp] menu button -> commands (WEBAPP_URL empty)", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 — a chat decoration must not stop a boot
        print(f"[webapp] menu button not set: {type(exc).__name__}: {exc}", flush=True)
        return False


def start_webapp(bot: telebot.TeleBot):
    """Serve the mini app from inside this process, after the workers exist.

    Order is not incidental: a start accepted before `start_workers` would
    enqueue into a queue nobody is popping yet. It is a short window, but it is
    exactly the window a restart puts users in.

    A web server that fails to bind must not take the bot down with it — the
    inline keyboards are the older and more important surface, and they do not
    need a socket. So this logs and returns instead of raising.
    """
    if not config.WEBAPP_URL:
        return None
    try:
        from webapp import server as webapp_server
    except Exception as exc:  # noqa: BLE001
        print(f"[webapp] not started (import): {exc}", flush=True)
        return None
    actions = BotActions(bot) if config.WEBAPP_ENABLE_ACTIONS else webapp_actions.ReadOnly()
    try:
        return webapp_server.serve_in_thread(
            config.WEBAPP_HOST, config.WEBAPP_PORT, actions=actions
        )
    except OSError as exc:
        # Almost always "address already in use": the standalone prototype
        # unit is still running on this port. Worth saying plainly, because
        # the symptom otherwise is a mini app that works and cannot start
        # anything — the old read-only server answering instead of this one.
        print(f"[webapp] not started: {exc} — is webapp.service still running?",
              flush=True)
        return None
    except Exception as exc:  # noqa: BLE001
        # Anything else at all. The inline keyboards are the older and more
        # important surface and they need no socket, so a web server that
        # cannot come up must not take the bot down with it.
        print(f"[webapp] not started: {type(exc).__name__}: {exc}", flush=True)
        return None








# --- live screen renderers (bot/screen.py) ---
#
# One editable message per user: every renderer rebuilds a screen from stored
# state, so a background thread can redraw the same message without knowing
# which handler drew it last.














































# Registered at import so anything that draws a screen (queue worker, payment
# poller, tests) works without waiting for create_bot().
register_screens()


def create_bot() -> telebot.TeleBot:
    global _active_bot
    if not config.BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Put it in .env or environment:\n"
            "  BOT_TOKEN=123456:ABC..."
        )
    n_threads = max(1, int(getattr(config, "BOT_HANDLER_THREADS", 2) or 2))
    bot = telebot.TeleBot(
        config.BOT_TOKEN,
        parse_mode="HTML",
        threaded=True,
        num_threads=n_threads,
    )
    _active_bot = bot
    # The group tier asks Telegram who is in SUBSCRIBER_GROUP; give it the
    # instance that is already authenticated instead of a second client.
    bot_group_tier.bind(bot)
    register_screens()
    print(f"TeleBot workers: num_threads={n_threads}", flush=True)

    # --- Per-user message cooldown -------------------------------------
    # One choke point for every incoming message, ahead of every handler:
    # telebot dispatches message updates through self.process_new_messages,
    # so replacing it on the instance gates commands, text, files and media
    # alike without touching a single handler. Callback queries keep their own
    # path — the UI is inline buttons, and gating those would freeze it.
    _dispatch_messages = bot.process_new_messages

    def _cooldown_passed(message: types.Message) -> bool:
        u = getattr(message, "from_user", None)
        if u is None:
            return True
        uid = int(u.id)
        if uid in config.ADMIN_IDS:
            return True
        ok, left, warn = bot_rate.message_gate(uid, config.MESSAGE_COOLDOWN_SEC)
        if ok:
            return True
        if warn:
            # Only the first message of a window gets an answer; the rest are
            # dropped in silence so a flood cannot turn into outbound traffic.
            try:
                lang = bot_db.get_lang(uid)
            except Exception:
                lang = "ru"
            safe_send(
                bot,
                message.chat.id,
                t("rate.cooldown", lang, seconds=int(left) + 1),
            )
        return False

    def _is_private(message: types.Message) -> bool:
        chat = getattr(message, "chat", None)
        return (getattr(chat, "type", "private") or "private") == "private"

    def _process_new_messages(messages) -> None:
        for m in messages:
            # Join/leave in SUBSCRIBER_GROUP → the tier moves with it, now.
            bot_group_tier.note_service_message(m)
        # Every screen this bot draws is one anchored message per user, and
        # every answer contains that user's wallets. In a group both are
        # wrong, so group and channel updates are read (for membership) and
        # never answered — the bot is a member of SUBSCRIBER_GROUP, not a
        # participant in it.
        allowed = [m for m in messages if _is_private(m) and _cooldown_passed(m)]
        if allowed:
            _dispatch_messages(allowed)

    bot.process_new_messages = _process_new_messages

    # Gate/screen helpers moved to handlers/common.py; bound here so the
    # handlers below still close over the same short names.
    import handlers.common as _common
    from functools import partial

    _screen = partial(_common.show_screen, bot)
    _admin_notify = partial(_common.admin_notify, bot)
    _screen_gate = partial(_common.screen_gate, bot)
    _gate_sub = partial(_common.gate_sub, bot)
    _is_admin = _common.is_admin
    _deny_if_banned = partial(_common.deny_if_banned, bot)
    _deny_if_banned_call = partial(_common.deny_if_banned_call, bot)
    _admin_edit = partial(_common.admin_edit, bot)

    # Handler groups split out of create_bot. They get the *running* module
    # object (tgbot or __main__) — see handlers.current_tgbot for why a plain
    # `import tgbot` is wrong here.
    import sys as _sys

    _tg_mod = _sys.modules.get("tgbot") or _sys.modules["__main__"]
    import handlers.admin as _h_admin
    import handlers.billing as _h_billing

    _h_admin.register(bot, _tg_mod)
    _h_billing.register(bot, _tg_mod)
    import handlers.commands as _h_commands
    import handlers.documents as _h_documents
    import handlers.profile as _h_profile
    import handlers.projects as _h_projects
    import handlers.text as _h_text

    _h_commands.register(bot, _tg_mod)
    _h_projects.register(bot, _tg_mod)
    _h_profile.register(bot, _tg_mod)
    _h_documents.register(bot, _tg_mod)
    _h_text.register(bot, _tg_mod)






































        # No auto-start: user must press «▶ Начать рег» (queue:start:<id>)
        # so Twitter/URL buttons do not enqueue by accident.
















    return bot


def _harden_secret_files() -> None:
    """Best-effort chmod 600 on secrets / proxy lists / subscriber id file."""
    files = [config.BASE_DIR / ".env"]
    for path in _proxy_paths():
        files.append(path)
    try:
        from bot import subscribers as _sub

        files.append(_sub.subscribers_path())
    except Exception:
        pass
    for p in files:
        try:
            if p.is_file():
                p.chmod(0o600)
        except OSError:
            pass


def main() -> None:
    bot_db.init_db()
    config.USER_PROXY_DIR.mkdir(parents=True, exist_ok=True)
    config.USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _harden_secret_files()
    if not config.ADMIN_IDS and not config.ALLOW_OPEN_ADMIN:
        print(
            "WARNING: ADMIN_IDS empty — admin commands denied (set ADMIN_IDS or ALLOW_OPEN_ADMIN=1)",
            flush=True,
        )
    projects = load_projects()
    if not projects:
        print("WARNING: no projects loaded from", config.PROJECTS_DIR)

    n_sub = bot_subscribers.load_subscribers()
    n_loaded = proxy_pool.loaded_count()
    n = len(get_proxies())
    src = ", ".join(str(p) for p in _proxy_paths()) or config.PROXY_FILE
    print(f"Loaded {n_loaded} shared proxies from: {src}")
    if not shared_enabled():
        print(
            f"[proxies] shared pool OFF — serving 0 (loaded {n_loaded} unused); "
            "users must upload /proxy",
            flush=True,
        )
    print(
        f"Subscribers: {n_sub} from {bot_subscribers.subscribers_path()} "
        f"(shared OK limit={config.SUBSCRIBER_OK_LIMIT})"
    )
    if bot_group_tier.enabled():
        print(
            f"Group tier: {config.SUBSCRIBER_GROUP} → OK limit="
            f"{bot_group_tier.member_ok_limit()} (bot must be its administrator)"
        )
    else:
        print("Group tier: off (SUBSCRIBER_GROUP empty)")
    projs = list_projects()
    if n == 0 and shared_enabled():
        print(
            "WARNING: empty shared proxies — free-tier users must upload own proxies; "
            "All projects require proxies (own or shared)."
        )
    print(
        f"Concurrency: submit={config.BOT_CONCURRENCY} "
        f"project={config.PROJECT_CONCURRENCY} "
        f"http={config.GLOBAL_HTTP_CONCURRENCY} "
        f"rpm={config.GLOBAL_REQUESTS_PER_MINUTE} "
        f"handler_threads={getattr(config, 'BOT_HANDLER_THREADS', 2)}"
    )
    print(
        f"Limits: free={bot_db.get_free_limit()} "
        f"(SQLite; admin /set_free_limit)"
    )

    names = ", ".join(f"{p.id}({p.name})" for p in projs)
    print(f"Projects: {names or '(none)'}")
    if config.REQUIRE_CHANNEL:
        print(
            f"Channel gate: ON  chat={config.REQUIRED_CHANNEL}  "
            f"url={config.REQUIRED_CHANNEL_URL}"
        )
        print(
            "NOTE: add this bot as administrator of the channel "
            "so getChatMember works."
        )
    else:
        print("Channel gate: OFF")

    bot = create_bot()
    start_project_source_watcher()
    bot_queue.start_workers(bot, queue_submit_one)
    if not shared_enabled():
        n_cancel = bot_queue.cancel_jobs_without_own_proxies(bot=bot)
        print(
            f"[proxies] shared pool OFF — cancelled {n_cancel} job(s) without own proxies",
            flush=True,
        )
    start_webapp(bot)
    set_menu_button(bot)
    set_bot_commands(bot)
    start_mint_scanner(bot)
    print(
        f"Bot starting (db={config.DB_PATH}, retries={config.RETRIES}, "
        f"retry_delay={config.RETRY_DELAY}, concurrency={config.BOT_CONCURRENCY})"
    )
    print(f"Submit queue workers: {config.BOT_CONCURRENCY}")

    def _admin_send(admin_id: int, text: str) -> None:
        bot.send_message(admin_id, text, parse_mode="HTML", disable_web_page_preview=True)

    bot_alerts.start_alert_thread(
        get_shared_count=lambda: len(get_proxies()),
        get_own_proxy_users=count_own_proxy_users,
        send=_admin_send,
    )

    # Outbound queue: resumes any batch a restart interrupted before it sends
    # anything new.
    bot_broadcast.start_sender(bot)
    try:
        # Finished rows are kept only long enough to answer «did it go out?».
        # ops/prune_db.py is project-scoped and cannot see this table.
        purged = bot_db.purge_broadcast_rows(14)
        if purged:
            print(f"[broadcast] purged {purged} finished row(s) older than 14d", flush=True)
    except Exception as e:
        print(f"[broadcast] purge: {e}", flush=True)
    pending_batches = bot_db.list_pending_broadcast_batches()
    if pending_batches:
        print(
            f"[broadcast] resuming {len(pending_batches)} batch(es): "
            f"{', '.join(pending_batches)}",
            flush=True,
        )
    # First pass also seeds the known-project list on a fresh database, which is
    # what stops a first run from announcing every project at once.
    try:
        check_new_projects()
    except Exception as e:
        print(f"[newproj] startup check: {e}", flush=True)

    if payments_np.is_enabled() and not billing.sales_open():
        # No min-amount probe and no "plan X is unsellable" alert: with the shop
        # shut both only produce noise. The poller still starts below.
        print(
            "[pay] SALES_ENABLED=0 — shop closed, no new payments; "
            f"{bot_db.count_active_subscriptions().get('total', 0)} active plans "
            "keep running until they expire",
            flush=True,
        )
        if bool(getattr(config, "PAYMENTS_ENABLED", False)):
            # Left on so a transfer already in flight to an open address is
            # still credited; with no new payments it simply idles.
            payments_poll.start_payment_poller(bot=bot, send=_admin_send)
    elif payments_np.is_enabled():
        # Best-effort: knowing the minimum decides whether Basic is sellable.
        try:
            minimum = payments_np.refresh_min_amount_cache()
            unsellable = [p.id for p in billing.unpurchasable_plans()]
            print(
                f"[np] {payments_np.api_base()} min=${minimum} "
                f"{'unsellable: ' + ','.join(unsellable) if unsellable else 'all plans sellable'}",
                flush=True,
            )
            if unsellable:
                for admin_id in config.ADMIN_IDS:
                    _admin_send(
                        admin_id,
                        "⚠️ NOWPayments минимум $"
                        f"{minimum} — тарифы {', '.join(unsellable)} нельзя оплатить.",
                    )
        except Exception as e:
            print(f"[np] startup check: {e}", flush=True)
        if bool(getattr(config, "PAYMENTS_ENABLED", False)):
            payments_poll.start_payment_poller(bot=bot, send=_admin_send)
        else:
            print("[pay] PAYMENTS_ENABLED=0 — poller off, paid UI hidden", flush=True)
    else:
        print("[pay] NOWPAYMENTS_API_KEY not set — paid UI hidden", flush=True)
    # Immediate first-pass (thread also re-checks each interval for next UTC days)
    try:
        bot_alerts.maybe_daily_digest(
            shared_proxy_count=len(get_proxies()),
            own_proxy_users=count_own_proxy_users(),
            send=_admin_send,
        )
    except Exception as e:
        print(f"[digest] {e}", flush=True)

    # threaded=True (default): long submit retries do not freeze other users.
    # long_polling_timeout: how long Telegram holds getUpdates (seconds).
    # timeout: HTTP client timeout base — must stay well above long_polling_timeout
    # so slow/unstable routes to api.telegram.org do not raise ReadTimeout.
    poll_timeout = 90
    long_poll = 25
    print(
        f"Polling: long_poll={long_poll}s  http_timeout={poll_timeout}s  "
        f"(network blips to api.telegram.org are retried automatically)"
    )
    print("Admin: /admin · alerts thread on · digest if ADMIN_DIGEST=1")
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=poll_timeout,
                long_polling_timeout=long_poll,
                interval=0,
                logger_level=logging.ERROR,
            )
        except KeyboardInterrupt:
            print("Bot stopped by user.")
            break
        except Exception as e:
            # Should be rare: infinity_polling already catches most errors and
            # continues. This outer loop is a last-resort restart.
            print(f"Polling crashed ({type(e).__name__}: {e}); restart in 5s…")
            time.sleep(5)
            continue
        # Clean exit of infinity_polling (stop_polling) — do not spin
        break


if __name__ == "__main__":
    main()
