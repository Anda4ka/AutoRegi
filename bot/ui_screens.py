"""Every screen the bot shows: keyboards, texts, renderers.

One place for the user-facing surface that is not a handler: the project
picker, the project screen, the queue dashboard fallback, the payment
screens. Bodies were moved verbatim from tgbot.py; handlers still live there
and re-import these names.

`get_project`/`list_projects` are called through the registry module on
purpose: tests patch `registry.get_project`, and a from-import binding would
freeze the patch out.
"""
from __future__ import annotations

import json

import telebot
from telebot import types

from bot import billing
from bot import billing_ux
from bot import config
from bot import privatka
from bot import db as bot_db
from bot import fresh as bot_fresh
from bot import limits as bot_limits
from bot import new_project_alert as bot_new_project
from bot import project_health as bot_project_health
from bot import proxy_pool
from bot import registry
from bot import screen
from bot import submit_queue as bot_queue
from bot import tweet_urls
from bot import user_data as user_data_mod
from bot import user_proxies as user_proxies_mod
from bot.channel import subscribe_keyboard, subscribe_text
from bot.i18n import noun_accounts, noun_registrations, short_wallet, t
from bot.registry import Project
from bot.ux import (
    esc,
    is_busy_detail,
    progress_bar,
    remaining,
)
from bot import errors as bot_errors

get_proxies = proxy_pool.get_proxies


def own_proxy_only_notice(lang: str) -> str:
    """Empty unless the shared pool is off — then the ops banner."""
    if proxy_pool.shared_enabled():
        return ""
    return t("ops.own_proxy_only", lang)


def no_proxies_text(lang: str, *, name: str) -> str:
    key = (
        "queue.no_proxies_own_only"
        if not proxy_pool.shared_enabled()
        else "queue.no_proxies"
    )
    return t(key, lang, name=name)


SUGGEST_MIN_LEN = 5

SUGGEST_MAX_LEN = 1000

def admin_keyboard_home() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
        types.InlineKeyboardButton("👤 Пользователь", callback_data="admin:user"),
    )
    kb.row(
        types.InlineKeyboardButton("🎚 Лимиты", callback_data="admin:limits"),
        types.InlineKeyboardButton("⚠️ Ошибки", callback_data="admin:errors"),
    )
    kb.row(
        types.InlineKeyboardButton("📁 Проекты", callback_data="admin:projects"),
        types.InlineKeyboardButton("🩺 Здоровье", callback_data="admin:health"),
    )
    kb.row(
        types.InlineKeyboardButton("💎 Подписки", callback_data="admin:subs"),
        types.InlineKeyboardButton("💳 Платежи", callback_data="admin:pay"),
    )
    kb.row(
        types.InlineKeyboardButton("🔄 Обновить", callback_data="admin:reload"),
        types.InlineKeyboardButton("📣 Рассылка", callback_data="admin:bcast"),
    )
    kb.add(types.InlineKeyboardButton("« Закрыть", callback_data="admin:close"))
    return kb

def admin_keyboard_payments() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "🔄 Проверить минимум", callback_data="admin:pay_min"
        )
    )
    kb.add(types.InlineKeyboardButton("« Назад", callback_data="admin:home"))
    return kb

# Live projects per page of the admin Projects screen. Two to a row, so a page
# is five rows plus the pager — the list used to be cut at twelve with no way to
# reach the rest, and with 23 live projects that hid half of them.
ADMIN_PROJECTS_PER_PAGE = 10

def admin_keyboard_projects(page: int = 0) -> types.InlineKeyboardMarkup:
    """Runtime-disabled projects → enable buttons; live projects → disable.

    The live half is paged: every project must be reachable, which is the whole
    point of a screen whose job is switching one off.
    """
    kb = types.InlineKeyboardMarkup()
    off_ids = set(bot_db.list_runtime_disabled_project_ids())
    # Enable buttons for RT-off. Never paged: it is the short list, and it is
    # the one an admin comes here for after an alert.
    for pid in sorted(off_ids):
        label = pid
        p = registry.get_project(pid, allow_runtime_disabled=True)
        if p is not None:
            label = p.name
        kb.add(
            types.InlineKeyboardButton(
                f"▶ On · {label}",
                callback_data=f"admin:proj_on:{pid}",
            )
        )
    live = registry.list_projects()
    per = max(1, ADMIN_PROJECTS_PER_PAGE)
    pages = max(1, (len(live) + per - 1) // per)
    page = max(0, min(int(page or 0), pages - 1))
    chunk = live[page * per : (page + 1) * per]
    for i in range(0, len(chunk), 2):
        row = [
            types.InlineKeyboardButton(
                f"⏹ {p.name}",
                callback_data=f"admin:proj_off:{p.id}",
            )
            for p in chunk[i : i + 2]
        ]
        kb.row(*row)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(
                types.InlineKeyboardButton(
                    "‹ Назад", callback_data=f"admin:projects:{page - 1}"
                )
            )
        nav.append(
            types.InlineKeyboardButton(
                f"стр. {page + 1}/{pages}", callback_data="admin:projects_noop"
            )
        )
        if page + 1 < pages:
            nav.append(
                types.InlineKeyboardButton(
                    "Вперёд ›", callback_data=f"admin:projects:{page + 1}"
                )
            )
        kb.row(*nav)
    on = bot_new_project.is_enabled()
    kb.add(
        types.InlineKeyboardButton(
            f"🆕 Алерт о новых проектах: {'вкл' if on else 'выкл'}",
            callback_data="admin:npa_toggle",
        )
    )
    pool_on = proxy_pool.shared_enabled()
    kb.add(
        types.InlineKeyboardButton(
            f"🌐 Общий пул прокси: {'вкл' if pool_on else 'выкл'}",
            callback_data="admin:sp_toggle",
        )
    )
    kb.add(types.InlineKeyboardButton("« Админ", callback_data="admin:home"))
    return kb

def admin_keyboard_health() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "🔄 Проверить сейчас", callback_data="admin:health_refresh"
        )
    )
    kb.add(types.InlineKeyboardButton("« Админ", callback_data="admin:home"))
    return kb

def admin_keyboard_back() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("« Админ", callback_data="admin:home"))
    return kb

def admin_keyboard_batch(batch_id: str) -> types.InlineKeyboardMarkup:
    """Progress / stop for one outbound batch (bot/broadcast.py)."""
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(
            "🔄 Прогресс", callback_data=f"admin:bstat:{batch_id}"
        ),
        types.InlineKeyboardButton(
            "🛑 Остановить", callback_data=f"admin:bstop:{batch_id}"
        ),
    )
    kb.add(types.InlineKeyboardButton("« Админ", callback_data="admin:home"))
    return kb

def admin_keyboard_user(uid: int, banned: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "Reset OK all", callback_data=f"admin:rst:{uid}"
        )
    )
    if banned:
        kb.add(
            types.InlineKeyboardButton(
                "Unban", callback_data=f"admin:unban:{uid}"
            )
        )
    else:
        kb.add(
            types.InlineKeyboardButton(
                "Ban", callback_data=f"admin:ban:{uid}"
            )
        )
    kb.add(types.InlineKeyboardButton("« Админ", callback_data="admin:home"))
    return kb

def admin_keyboard_limits() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Set free", callback_data="admin:setfree"),
    )
    kb.add(
        types.InlineKeyboardButton("Set VIP uid N", callback_data="admin:setvip")
    )
    kb.add(types.InlineKeyboardButton("« Админ", callback_data="admin:home"))
    return kb

def admin_keyboard_reload() -> types.InlineKeyboardMarkup:
    """One-shot full reload (proxies + subscribers + projects)."""
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "🔄 Reload all", callback_data="admin:relall"
        )
    )
    kb.add(types.InlineKeyboardButton("« Админ", callback_data="admin:home"))
    return kb

def new_project_preview_keyboard(project_id: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    # The admin callback handler only listens on the «admin:» prefix.
    kb.row(
        types.InlineKeyboardButton(
            "📣 Разослать", callback_data=f"admin:npa:go:{project_id}"
        ),
        types.InlineKeyboardButton(
            "✖️ Не слать", callback_data=f"admin:npa:no:{project_id}"
        ),
    )
    return kb

def _project_button_label(
    name: str, ok_n: int, lim: int, health_status: str | None = None
) -> str:
    """
    Compact inline button text:
      🔒 Name             — limit exhausted
      🟢 Name · N/L       — health + progress
      ⚪ Name             — not checked yet
    """
    name = (name or "?").strip() or "?"
    if lim > 0 and ok_n >= lim:
        label = f"🔒 {name} · {ok_n}/{lim}"
    elif ok_n > 0:
        label = (
            f"{bot_project_health.status_icon(health_status)} "
            f"{name} · {ok_n}/{lim}"
        )
    else:
        label = f"{bot_project_health.status_icon(health_status)} {name}"
    if len(label) > 60:
        label = label[:57] + "…"
    return label

def _projects_per_page() -> int:
    return max(1, int(getattr(config, "PROJECTS_PER_PAGE", 8) or 8))

def project_sort_key(
    project: Project, ok_n: int, health_status: str | None, fresh: int | None = None
) -> tuple:
    """Open first, then most work left, then newest.

    `order` counts upward as projects are added, so sorting on it ascending —
    which this did — put the oldest project at the top of the list every time.
    Negated, the newest arrival leads.

    `fresh` is how many of this user's wallets the project has not seen. When
    the caller knows it, a project with nothing left to send sinks below one
    that still has work, which is the only ordering that matches what the
    buttons will actually do. Callers that do not know it pass None and get the
    old "untouched first" behaviour.
    """
    is_open = 0 if str(health_status or "").lower() in ("open", "reachable") else 1
    if fresh is None:
        work = 0 if ok_n <= 0 else 1
    else:
        work = 0 if fresh > 0 else 1
    # Work before health, and that order is deliberate. `is_open` only
    # separates "checked and reachable" from "not checked yet" — the genuinely
    # broken ones are already excluded by `can_start`. Ranking health first put
    # a finished project above an unchecked one with twenty wallets waiting,
    # which is the wrong way round: a never-checked project after a restart is
    # not a worse bet than one there is nothing left to do in.
    return (work, is_open, -int(getattr(project, "order", 0) or 0), project.name.lower())

def partition_projects(
    projects: list[Project], counts: dict[str, int], lim: int, health: dict
) -> tuple[list[tuple], list[tuple]]:
    """Split into (available, locked); each item is (project, ok_n, status)."""
    available: list[tuple] = []
    locked: list[tuple] = []
    for p in projects:
        ok_n = int(counts.get(p.id, 0) or 0)
        status = (health.get(p.id) or {}).get("status")
        (locked if lim > 0 and ok_n >= lim else available).append((p, ok_n, status))
    available.sort(key=lambda item: project_sort_key(item[0], item[1], item[2]))
    return available, locked

def projects_page(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    """Return (page items, clamped page index, total pages)."""
    pages = max(1, -(-len(items) // max(1, per_page)))
    page = max(0, min(int(page), pages - 1))
    start = page * per_page
    return items[start : start + per_page], page, pages

def projects_keyboard(
    lang: str = "ru",
    user_id: int | None = None,
    *,
    page: int = 0,
    show_locked: bool = False,
) -> types.InlineKeyboardMarkup:
    """
    Compact project picker: one page of buttons, two per row.

    With more than a dozen projects a flat list is a wall of buttons, so the
    open and not-yet-started ones come first, the rest paginate, and projects
    where the user has run out of slots move behind a single «completed»
    button instead of padding every page.

    Projects are ALWAYS reachable so the direct-text and bulk-file flows still
    work. When the user has no uploaded accounts yet, a prominent
    «Upload accounts» CTA is placed above the project list.
    """
    kb = types.InlineKeyboardMarkup(row_width=2)

    has_data = bool(user_id is not None and user_data_mod.has_user_data(user_id))
    if not has_data:
        # Upload-first CTA on top; projects remain visible below it so a user
        # can still pick one and send a single text line / bulk .txt.
        kb.add(
            types.InlineKeyboardButton(
                text=t("btn.upload_data", lang),
                callback_data="data:menu",
            )
        )

    projects = bot_limits.visible_projects(
        registry.list_projects(), user_id
    )  # sorted (order, name)
    counts: dict[str, int] = {}
    if user_id is not None:
        current_db = __import__("bot.db", fromlist=["get_all_ok_counts"])
        counts = current_db.get_all_ok_counts(int(user_id))
    lim = (
        bot_limits.effective_ok_limit(int(user_id))
        if user_id is not None
        else __import__("bot.db", fromlist=["get_free_limit"]).get_free_limit()
    )
    health = bot_project_health.cached_health_map(projects)
    available, locked = partition_projects(projects, counts, lim, health)

    items = locked if show_locked else available
    page_items, page, pages = projects_page(items, page, _projects_per_page())
    row: list[types.InlineKeyboardButton] = []
    for p, ok_n, status in page_items:
        row.append(
            types.InlineKeyboardButton(
                text=_project_button_label(p.name, ok_n, lim, status),
                callback_data=f"project:{p.id}",
            )
        )
        if len(row) == 2:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)

    if pages > 1:
        nav = [
            types.InlineKeyboardButton(
                text=t("btn.page_prev", lang),
                callback_data=f"projects:page:{(page - 1) % pages}",
            ),
            types.InlineKeyboardButton(
                text=t("btn.page_at", lang, page=page + 1, pages=pages),
                callback_data="noop",
            ),
            types.InlineKeyboardButton(
                text=t("btn.page_next", lang),
                callback_data=f"projects:page:{(page + 1) % pages}",
            ),
        ]
        kb.row(*nav)

    if show_locked:
        kb.add(
            types.InlineKeyboardButton(
                text=t("btn.locked_hide", lang), callback_data="projects:locked:0"
            )
        )
    elif locked:
        kb.add(
            types.InlineKeyboardButton(
                text=t("btn.locked_show", lang, n=len(locked)),
                callback_data="projects:locked:1",
            )
        )

    # VIP: one tap queues every project that is open and still has slots. It
    # sits under the list (not on top) so it is never the button a user hits
    # while reaching for a project.
    # limit_mode reads the cached tier (this runs on every render); the
    # authoritative is_vip() check happens again when the batch starts.
    if (
        user_id is not None
        and has_data
        and bot_limits.limit_mode(int(user_id)) == "vip"
    ):
        # Count from the data this render already has (health + slots); the
        # authoritative per-project gate runs at start time.
        ready = sum(
            1
            for _p, _ok, status in available
            if str(status or "").lower() not in ("closed", "config")
        )
        if ready:
            kb.add(
                types.InlineKeyboardButton(
                    text=t("btn.queue_all", lang, n=ready),
                    callback_data="queue:all",
                )
            )

    # Utility buttons (kept compact, two per row).
    kb.row(
        types.InlineKeyboardButton(
            text=t("btn.queue", lang),
            callback_data="queue:status",
        ),
        types.InlineKeyboardButton(
            text=t("btn.data", lang),
            callback_data="data:menu",
        ),
    )
    # Mini App (webapp/): live progress, which an edited message cannot show at
    # this refresh rate. It sits beside the existing queue button rather than
    # replacing it — the inline flow stays whole, and clearing WEBAPP_URL puts
    # the bot back exactly as it was.
    if config.WEBAPP_URL:
        kb.row(
            types.InlineKeyboardButton(
                text=t("btn.webapp", lang),
                web_app=types.WebAppInfo(url=config.WEBAPP_URL),
            )
        )
    kb.row(
        types.InlineKeyboardButton(
            text=t("btn.proxy", lang),
            callback_data="proxy:menu",
        ),
        types.InlineKeyboardButton(
            text=t("btn.lang", lang),
            callback_data="lang:menu",
        ),
    )
    kb.add(
        types.InlineKeyboardButton(
            text=t("btn.suggest", lang),
            callback_data="suggest:start",
        )
    )
    if privatka.enabled():
        kb.add(privatka.nav_button())
    return kb

def language_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(t("lang.btn_ru", "ru"), callback_data="lang:ru"),
        types.InlineKeyboardButton(t("lang.btn_en", "en"), callback_data="lang:en"),
    )
    return kb

def proxy_keyboard(*, has_own: bool, lang: str = "ru") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    if has_own:
        kb.row(
            types.InlineKeyboardButton(
                text=t("btn.proxy_check", lang),
                callback_data="proxy:check",
            )
        )
        kb.add(
            types.InlineKeyboardButton(
                text=t("proxy.delete", lang),
                callback_data="proxy:clear",
            )
        )
    kb.add(
        types.InlineKeyboardButton(
            text=t("btn.back", lang),
            callback_data="projects:back",
        )
    )
    return kb

def suggest_prompt_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            text="« Отмена",
            callback_data="suggest:cancel",
        )
    )
    return kb

def suggest_prompt_text() -> str:
    return (
        "💡 <b>Предложить проект</b>\n\n"
        "Напиши <b>одним сообщением</b> название и ссылку "
        "(Twitter / сайт / mint — что есть).\n\n"
        "Пример:\n"
        "<code>Cool NFT https://x.com/cool_nft</code>\n\n"
        f"Лимит: <b>1 предложение в сутки</b> (UTC) на аккаунт.\n"
        f"Длина: {SUGGEST_MIN_LEN}–{SUGGEST_MAX_LEN} символов.\n\n"
        "Отмена — кнопка ниже или /start."
    )

def project_screen_keyboard(
    project: Project,
    lang: str = "ru",
    *,
    user_id: int | None = None,
    ok_count: int | None = None,
) -> types.InlineKeyboardMarkup:
    """
    Project screen keyboard.

    Primary action depends on readiness (see _project_start_state):
      • can start (data + free slots + proxies) → ▶ Start registration
      • no accounts                             → 📤 Upload accounts
      • slots exhausted / no proxies            → no start button (links + back)
    Then X / site URL buttons, optional «Edit accounts», and «To projects».
    Opening a link never enqueues (explicit start button only).
    """
    kb = types.InlineKeyboardMarkup()
    if user_id is None:
        st = None
    else:
        if ok_count is None:
            ok_count = bot_db.get_ok_count(user_id, project.id)
        st = _project_start_state(project, ok_count, user_id)

    if st is not None and st["can_start"] and st["self_sourced"]:
        # One tap spends a whole batch of slots, so ask before starting.
        kb.add(
            types.InlineKeyboardButton(
                text=t("btn.start_wallets", lang, n=st["will_send"]),
                callback_data=f"queue:ask:{project.id}",
            )
        )
    elif st is not None and st["can_start"]:
        n = st["will_send"]
        kb.add(
            types.InlineKeyboardButton(
                text=t(
                    "btn.start_reg",
                    lang,
                    n=n,
                    reg=noun_registrations(n, lang, accusative=True),
                ),
                callback_data=f"queue:start:{project.id}",
            )
        )
    elif st is not None and not st["has_data"]:
        # No accounts yet → guide to upload.
        kb.add(
            types.InlineKeyboardButton(
                text=t("btn.upload_data", lang),
                callback_data="data:menu",
            )
        )
    # else: limit reached or no proxies — no start button; the screen text
    # explains why (limit done / no proxies).

    tw = (project.twitter_url or "").strip()
    site = (project.link or "").strip()
    link_row: list[types.InlineKeyboardButton] = []
    if tw:
        link_row.append(types.InlineKeyboardButton(text=t("btn.twitter", lang), url=tw))
    if site and site != tw:
        link_row.append(types.InlineKeyboardButton(text=t("btn.site", lang), url=site))
    if link_row:
        kb.row(*link_row)
    # The mint often lives somewhere other than the registration page
    # (a countdown, a /mint route, an OpenSea collection).
    mint_url = str(getattr(project, "mint_url", "") or "").strip()
    if mint_url and mint_url not in (tw, site):
        kb.add(types.InlineKeyboardButton(text=t("btn.mint", lang), url=mint_url))
    if st is not None and st["has_data"] and not st["self_sourced"]:
        kb.add(
            types.InlineKeyboardButton(
                text=t("btn.change_data", lang),
                callback_data="data:menu",
            )
        )
    if user_id is not None and int(ok_count or 0) > 0:
        # «Мои регистрации»: the user's own OK/ALREADY rows as a file.
        # Only offered when there is something to export.
        kb.add(
            types.InlineKeyboardButton(
                text=t("btn.export_results", lang),
                callback_data=f"results:{project.id}",
            )
        )
    if getattr(project, "queue_redeliver", None) is not None:
        # A delivery that failed (blocked bot, timeout) must not strand the
        # artifact — the user can always ask for it again.
        kb.add(
            types.InlineKeyboardButton(
                text=t("btn.get_keys", lang),
                callback_data=f"project_deliver:{project.id}",
            )
        )
    kb.add(
        types.InlineKeyboardButton(
            text=t("btn.to_projects", lang),
            callback_data="projects:back",
        )
    )
    return kb

def _req_label_for_column(key: str, label: str, lang: str) -> str:
    """Map a bulk_columns field to a friendly «what you need» label."""
    kl = (key or "").lower().strip()
    if kl in ("handle", "twitter", "x_username", "xhandle", "username"):
        return t("req.handle", lang)
    if kl == "wallet":
        return t("req.wallet", lang)
    if tweet_urls.is_url_field_key(key):
        return t("req.link", lang)
    return label or key

def welcome_text(
    first_name: str | None,
    projects: list[Project],
    user_id: int | None = None,
    lang: str = "ru",
) -> str:
    """Compact contextual /start with current state and a health legend."""
    name = f", {esc(first_name)}" if first_name else ""
    if not projects:
        parts = [t("welcome.hello", lang, name=name)]
        notice = own_proxy_only_notice(lang)
        if notice:
            parts.extend(["", notice])
        parts.extend(["", t("welcome.no_projects", lang)])
        return "\n".join(parts)
    del projects  # projects live only in the inline keyboard (no text duplicate)
    lim = (
        bot_limits.effective_ok_limit(user_id)
        if user_id is not None
        else bot_db.get_free_limit()
    )
    n_data = user_data_mod.count_user_data(user_id) if user_id is not None else 0
    active = bot_queue.user_active_jobs(user_id) if user_id is not None else []
    selected = (
        bot_db.get_selected_project(user_id) if user_id is not None else None
    )
    has_project = bool(selected and registry.get_project(selected) is not None)

    accounts_value = str(n_data) if n_data > 0 else t("welcome.accounts_none", lang)
    project_value = (
        esc(bot_queue.project_display_name(selected))
        if has_project
        else t("welcome.project_none", lang)
    )
    lines = [
        t("welcome.hello", lang, name=name),
        "",
    ]
    notice = own_proxy_only_notice(lang)
    if notice:
        lines.extend([notice, ""])
    lines.extend(
        [
            t("welcome.accounts_compact", lang, value=accounts_value),
            t("welcome.project_compact", lang, value=project_value),
        ]
    )
    if active:
        j = active[0]
        pname = esc(bot_queue.project_display_name(j.project_id))
        lines.append(
            t("welcome.active_queue_run", lang, name=pname, done=j.done, total=j.total)
        )
    else:
        lines.append(t("welcome.active_queue_none", lang))
    tier = bot_limits.limit_mode(user_id) if user_id is not None else "free"
    lines.append(
        t(
            "welcome.limit_compact",
            lang,
            n=lim,
            tier=t(f"tier.{tier}", lang) if tier else "",
        )
    )
    if user_id is not None:
        basic_line = privatka.basic_until_line(user_id)
        if basic_line:
            lines.append(basic_line)
    lines.extend(
        [
            "",
            t("welcome.health_legend", lang),
            t("welcome.pick", lang),
        ]
    )
    return "\n".join(lines)

def _proxy_readiness(user_id: int) -> tuple[str, int]:
    """
    Resolve the effective proxy pool for this user, mirroring
    user_proxies.resolve_proxies (own valid → shared → none).

    Returns (source, count): source in {"own","shared","corrupt","none"}.
    """
    own_urls = user_proxies_mod.load_user_proxy_urls(user_id)
    if own_urls:
        return "own", len(own_urls)
    if user_proxies_mod.own_proxy_file_exists(user_id):
        # Non-empty own file that fails to parse → blocked (no shared fallback).
        return "corrupt", 0
    shared = get_proxies()
    if shared:
        return "shared", len(shared)
    return "none", 0

def _project_start_state(
    project: Project,
    ok_count: int,
    user_id: int,
    *,
    rows: list | None = None,
    wallet_set: set[str] | None = None,
) -> dict:
    """Shared readiness state for the project screen text + keyboard.

    ``rows`` and ``wallet_set`` exist for the two callers that walk every
    project. `load_user_data_rows` is cached but hands back a fresh ``list``
    every time, so a twenty-project sweep of a thousand-line accounts file
    copied twenty thousand row objects per screen refresh — four seconds
    apart, per open app. Passing the list in reads it once and lowercases it
    once. A single-project caller can ignore both.
    """
    lim = bot_limits.effective_ok_limit(user_id)
    left = remaining(ok_count, lim)
    self_sourced = bool(getattr(project, "self_sourced", False))
    batch = int(getattr(project, "batch_size", 0) or 0)
    if self_sourced:
        # Nothing to upload: the project mints its own rows. A fixed batch is
        # all-or-nothing, so Start is offered only with a full batch of slots.
        has_data = True
        n_data = batch or left
        will_send = batch or left
        fresh = will_send
    else:
        if rows is None:
            rows = user_data_mod.load_user_data_rows(user_id)
        has_data = bool(rows)
        n_data = len(rows)
        # Rows the queue would *actually* send. This used to be
        # `min(n_data, left)` — the count before dedup — with a comment saying
        # dedup only lowers it further. On a mature account that understates
        # nothing and overstates everything: measured on the operator's list,
        # 18 of 21 projects had all 695 wallets already registered, so the
        # screen offered eighteen Start buttons that every one of them would
        # have refused. `bot/fresh.py` caches the honest number so asking is
        # affordable.
        fresh = bot_fresh.fresh_count(
            user_id, project.id, [r.wallet for r in rows],
            wallet_set=wallet_set) if has_data else 0
        will_send = min(fresh, left) if (has_data and left > 0) else 0
    proxy_src, proxy_n = _proxy_readiness(user_id)
    cached_health = bot_project_health.get_cached(project.id) or {}
    health_status = str(cached_health.get("status") or "unknown")
    health_blocked = health_status in ("config", "closed")
    allow_direct = bool(getattr(project, "allow_direct", False))
    has_proxies = proxy_src in ("own", "shared") or allow_direct
    can_start = bool(has_data and left > 0 and has_proxies and not health_blocked)
    if self_sourced and will_send > 0:
        can_start = can_start and left >= will_send
    elif not self_sourced:
        # Nothing left to send is not "ready with zero rows" — it is done.
        can_start = can_start and will_send > 0
    return {
        "lim": lim,
        "left": left,
        "fresh": fresh if not self_sourced else will_send,
        "self_sourced": self_sourced,
        "batch": batch,
        "has_data": has_data,
        "n_data": n_data,
        "will_send": will_send,
        "proxy_src": proxy_src,
        "proxy_n": proxy_n,
        "has_proxies": has_proxies,
        "allow_direct": allow_direct,
        "health_status": health_status,
        "health_blocked": health_blocked,
        "can_start": can_start,
    }

def _project_mint_info(project: Project, lang: str) -> str:
    """Mint line for the project screen; EN falls back to the RU text."""
    ru = str(getattr(project, "mint_info", "") or "").strip()
    en = str(getattr(project, "mint_info_en", "") or "").strip()
    if lang == "en":
        return en or ru
    return ru or en

def project_ready_text(
    project: Project, ok_count: int, user_id: int, lang: str | None = None
) -> str:
    """Project screen: name, what will be sent, slots, proxy, requirements."""
    lang = lang or bot_db.get_lang(user_id)
    st = _project_start_state(project, ok_count, user_id)
    left = st["left"]

    lines = [f"<b>{esc(project.name)}</b>", ""]
    notice = own_proxy_only_notice(lang)
    if notice:
        lines.extend([notice, ""])

    # What will be sent (honest, slot-capped number). Skip when the limit is
    # already reached — the «limit reached» line conveys that instead.
    if st["self_sourced"]:
        lines.append(t("project.selfsourced_send", lang, n=st["will_send"]))
        if left < st["will_send"]:
            lines.append(
                t("project.selfsourced_need", lang, need=st["will_send"], left=left)
            )
    elif st["has_data"] and left > 0:
        if st["will_send"] < st["n_data"]:
            lines.append(
                t(
                    "project.will_send_capped",
                    lang,
                    n=st["will_send"],
                    total=st["n_data"],
                    acc=noun_accounts(st["will_send"], lang),
                )
            )
        else:
            lines.append(
                t(
                    "project.will_send",
                    lang,
                    n=st["will_send"],
                    acc=noun_accounts(st["will_send"], lang),
                )
            )
    # Slots
    lines.append(t("project.slots", lang, n=left))
    if left <= 0:
        lines.append(t("project.limit_done", lang))
    if st["health_status"] == "config":
        lines.append(t("project.health_config", lang))
    elif st["health_status"] == "closed":
        lines.append(t("project.health_closed", lang))

    # Proxy readiness (actual pool, not just the tier label)
    ps = st["proxy_src"]
    if ps == "own":
        lines.append(t("project.proxy_own_n", lang, n=st["proxy_n"]))
    elif ps == "shared":
        lines.append(t("project.proxy_shared_n", lang, n=st["proxy_n"]))
    elif ps == "corrupt":
        lines.append(t("project.proxy_corrupt", lang))
    else:
        lines.append(t("project.proxy_none", lang))
        if not st["allow_direct"]:
            hint_key = (
                "project.proxy_none_hint_own_only"
                if not proxy_pool.shared_enabled()
                else "project.proxy_none_hint"
            )
            lines.append(t(hint_key, lang))

    # Mint announcement (project.toml `mint_info`): dates and phases move, so
    # this is what the project published, escaped and shown as plain text.
    mint = _project_mint_info(project, lang)
    if mint:
        lines.append(t("project.mint", lang, info=esc(mint)))

    # Requirements checklist (required bulk_columns → friendly labels).
    # Neutral bullet so it reads as «needed», not «already validated».
    req_items: list[str] = []
    for key, req, lab in (project.bulk_columns or []):
        if req:
            req_items.append(_req_label_for_column(key, lab, lang))
    seen: set[str] = set()
    req_items = [x for x in req_items if not (x in seen or seen.add(x))]
    if req_items:
        lines.append("")
        lines.append(t("project.requirements", lang))
        for item in req_items:
            lines.append(f"• {esc(item)}")

    if not st["has_data"] and not st["self_sourced"]:
        lines.append("")
        lines.append(t("project.no_data_hint", lang))

    # Detailed format help (project-specific) below.
    try:
        help_html = (project.help_html() or "").strip()
    except Exception:
        help_html = ""
    if help_html:
        lines.append("")
        lines.append(help_html)

    return "\n".join(lines)

def proxy_status_text(user_id: int, lang: str | None = None) -> str:
    lang = lang or bot_db.get_lang(user_id)
    mode = bot_limits.limit_mode(user_id)
    lim = bot_limits.effective_ok_limit(user_id)
    has_own = user_proxies_mod.has_own_proxies(user_id)
    lines = [t("proxy.title", lang), ""]
    notice = own_proxy_only_notice(lang)
    if notice:
        lines.extend([notice, ""])
    pool_on = proxy_pool.shared_enabled()
    if mode == "vip":
        if has_own:
            n = len(user_proxies_mod.load_user_proxy_urls(user_id))
            lines.append(t("proxy.vip_own", lang, n=n, limit=lim))
        elif pool_on:
            shared_n = len(get_proxies())
            lines.append(t("proxy.vip_shared", lang, n=shared_n, limit=lim))
        else:
            lines.append(t("proxy.shared_off", lang))
        lines.append(
            t("proxy.vip_note_own_only" if not pool_on else "proxy.vip_note", lang)
        )
    elif has_own:
        n = len(user_proxies_mod.load_user_proxy_urls(user_id))
        lines.append(t("proxy.own", lang, n=n))
    elif pool_on:
        shared_n = len(get_proxies())
        lines.append(t("proxy.shared", lang, n=shared_n, free=lim))
        lines.append(t("proxy.own_tip", lang))
    else:
        lines.append(t("proxy.shared_off", lang))
    health = bot_db.user_proxy_health(user_id)
    if health["fails_proxy"] > 0:
        lines.append("")
        lines.append(
            t(
                "proxy.health",
                lang,
                fails_proxy=health["fails_proxy"],
                fails=health["fails"],
                rows=health["rows"],
            )
        )
    lines.append("")
    lines.append(
        t(
            "proxy.howto",
            lang,
            max_lines=config.USER_PROXY_MAX_LINES,
            max_kb=max(1, config.USER_PROXY_MAX_BYTES // 1024),
        )
    )
    return "\n".join(lines)

def status_text(user_id: int, lang: str | None = None) -> str:
    lang = lang or bot_db.get_lang(user_id)
    projects = bot_limits.visible_projects(registry.list_projects(), user_id)
    counts = bot_db.get_all_ok_counts(user_id)
    selected = bot_db.get_selected_project(user_id)
    lines = [t("status.title", lang), ""]
    if not projects:
        lines.append(t("status.none", lang))
        return "\n".join(lines)
    for p in projects:
        ok = counts.get(p.id, 0)
        lim = bot_limits.effective_ok_limit(user_id)
        left = remaining(ok, lim)
        mark = t("status.selected", lang) if selected == p.id else ""
        lines.append(
            t(
                "status.line",
                lang,
                name=esc(p.name),
                ok=ok,
                limit=lim,
                left=left,
                mark=mark,
            )
        )
    if selected and registry.get_project(selected) is None:
        lines.append("")
        lines.append(t("status.stale", lang, id=esc(selected)))
    return "\n".join(lines)

def default_result_message(
    project: Project,
    status: str,
    detail: str,
    fields: dict,
    new_ok: int,
    user_id: int,
    lang: str | None = None,
) -> str:
    lang = lang or bot_db.get_lang(user_id)
    lim = bot_limits.effective_ok_limit(user_id)
    left = remaining(new_ok, lim)
    handle = fields.get("handle") or fields.get("twitter") or "—"
    wallet = fields.get("wallet") or "—"
    h_show = esc(handle)
    w_show = esc(short_wallet(str(wallet)))
    dlow = (detail or "").lower()
    is_dup = any(
        x in dlow
        for x in ("duplicate", "23505", "unique constraint", "already", "allowlist_wallet")
    )
    pname = esc(project.name)
    busy = status == "BUSY" or is_busy_detail(detail)

    if status == "OK":
        return t(
            "result.ok",
            lang,
            name=pname,
            handle=h_show,
            wallet=w_show,
            left=left,
            limit=lim,
        )
    if status == "ALREADY" or (status == "BAD_RESP" and is_dup):
        return t(
            "result.already",
            lang,
            name=pname,
            handle=h_show,
            ok=new_ok,
            limit=lim,
        )
    if busy:
        return t("result.busy", lang, name=pname, ok=new_ok, limit=lim)
    if status == "BAD_RESP":
        # Human sentence only — the raw body stays in submissions/log/admin.
        reason = bot_errors.user_message("BAD_RESP", detail, lang=lang)
        return t(
            "result.bad",
            lang,
            name=pname,
            reason=reason,
            ok=new_ok,
            limit=lim,
        )
    return t("result.fail", lang, name=pname, ok=new_ok, limit=lim)

def _mint_keyboard(proposal_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Принять", callback_data=f"mint:ok:{proposal_id}"),
        types.InlineKeyboardButton("✖️ Отклонить", callback_data=f"mint:no:{proposal_id}"),
    )
    kb.row(types.InlineKeyboardButton("✏️ Править", callback_data=f"mint:ed:{proposal_id}"))
    return kb

def _queue_start_markup(
    user_id: int,
    lang: str,
    *,
    primary_job: object | None = None,
) -> types.InlineKeyboardMarkup | None:
    """
    Keyboard for /queue: cancel active job + optional «Start/Enqueue» for selected project.
    Never auto-starts — user must press the button to add the next project.
    """
    kb = types.InlineKeyboardMarkup()
    rows_added = False
    if primary_job is not None:
        ck = bot_queue.progress_keyboard(primary_job, lang=lang)
        if ck is not None:
            for row in ck.keyboard:
                kb.row(*row)
                rows_added = True

    n_data = user_data_mod.count_user_data(user_id)
    pid = bot_db.get_selected_project(user_id)
    project = registry.get_project(pid) if pid else None
    if n_data > 0 and project is not None:
        # Start is offered only when actually ready (accounts + free slots +
        # proxies) — same state the project screen uses, so /queue cannot
        # bypass the readiness gate.
        ok_count = bot_db.get_ok_count(user_id, project.id)
        st = _project_start_state(project, ok_count, user_id)
        # Don't offer start if same project already active or queue full
        same = bot_queue.user_has_project_job(user_id, project.id)
        can = bot_queue.can_enqueue_user(user_id)
        if st["can_start"] and same is None and can:
            n_active = bot_queue.user_active_job_count(user_id)
            if n_active > 0:
                label = t(
                    "btn.enqueue_reg",
                    lang,
                    name=project.name,
                    n=st["will_send"],
                )
            else:
                label = t(
                    "btn.start_reg",
                    lang,
                    n=st["will_send"],
                    reg=noun_registrations(st["will_send"], lang, accusative=True),
                )
            # Telegram button text limit ~64 chars
            if len(label) > 60:
                label = label[:57] + "…"
            kb.add(
                types.InlineKeyboardButton(
                    text=label,
                    callback_data=f"queue:start:{project.id}",
                )
            )
            rows_added = True
    return kb if rows_added else None

def queue_status_or_prompt(
    bot: telebot.TeleBot,
    *,
    user_id: int,
    chat_id: int,
    lang: str = "ru",
) -> tuple[str, object | None]:
    """
    Active jobs → summary + cancel + optional «enqueue selected project».
    Idle + /data + selected project → prompt with Start button (no auto-start).
    Returns (html, markup_or_none).
    """
    del bot, chat_id  # status-only; start is via queue:start callback
    jobs = bot_queue.user_active_jobs(user_id)
    if jobs:
        primary = bot_queue.user_has_active_job(user_id)
        summary = bot_queue.format_user_jobs_summary(user_id, lang=lang)
        body = bot_queue.format_job_status(primary, lang=lang) if primary else ""
        text = summary
        if body:
            text = summary + "\n\n" + body
        n_data = user_data_mod.count_user_data(user_id)
        pid = bot_db.get_selected_project(user_id)
        project = registry.get_project(pid) if pid else None
        if (
            n_data > 0
            and project is not None
            and bot_queue.user_has_project_job(user_id, project.id) is None
            and bot_queue.can_enqueue_user(user_id)
        ):
            text += "\n\n" + t(
                "queue.can_enqueue",
                lang,
                name=esc(project.name),
                n=n_data,
            )
        return text, _queue_start_markup(
            user_id, lang, primary_job=primary
        )

    n_data = user_data_mod.count_user_data(user_id)
    pid = bot_db.get_selected_project(user_id)
    project = registry.get_project(pid) if pid else None

    if n_data > 0 and project is not None:
        text = t(
            "queue.ready_start",
            lang,
            name=esc(project.name),
            n=n_data,
        )
        return text, _queue_start_markup(user_id, lang)

    if n_data > 0:
        return t("queue.idle_need_project", lang, n=n_data), None
    if project is not None:
        return t(
            "queue.idle_need_data",
            lang,
            project=esc(project.name),
        ), None
    return t("queue.none", lang), None

# Back-compat alias for older call sites
queue_status_or_autostart = queue_status_or_prompt

def _screen_first_name(user_id: int) -> str:
    try:
        row = bot_db.get_user_row(user_id) or {}
    except Exception:
        row = {}
    return str(row.get("first_name") or "")

def render_home(user_id: int, lang: str, params: dict) -> screen.View:
    notice = str(params.get("notice") or "")
    text = welcome_text(
        _screen_first_name(user_id),
        bot_limits.visible_projects(registry.list_projects(), user_id),
        user_id=user_id,
        lang=lang,
    )
    if notice:
        text = f"{notice}\n\n{text}"
    try:
        page = int(params.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    return screen.View(
        text,
        projects_keyboard(
            lang,
            user_id=user_id,
            page=page,
            show_locked=bool(params.get("locked")),
        ),
    )

def render_gate(user_id: int, lang: str, params: dict) -> screen.View:
    del user_id
    return screen.View(
        str(params.get("text") or "") or subscribe_text(lang=lang),
        subscribe_keyboard(lang=lang),
    )

def render_project(user_id: int, lang: str, params: dict) -> screen.View:
    project = registry.get_project(str(params.get("pid") or ""))
    if project is None:
        return render_home(user_id, lang, {})
    ok = bot_db.get_ok_count(user_id, project.id)
    if params.get("confirm"):
        return _project_confirm_view(project, ok, user_id, lang)
    return screen.View(
        project_ready_text(project, ok, user_id, lang=lang),
        project_screen_keyboard(project, lang=lang, user_id=user_id, ok_count=ok),
    )

def _project_confirm_view(
    project: Project, ok_count: int, user_id: int, lang: str
) -> screen.View:
    """Second tap for self-sourced projects: a batch spends slots in one go."""
    st = _project_start_state(project, ok_count, user_id)
    n = st["will_send"]
    text = "\n".join([
        f"<b>{esc(project.name)}</b>",
        "",
        t("project.confirm_body", lang, n=n, left=st["left"]),
        "",
        t("project.confirm_keys", lang),
    ])
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            text=t("btn.confirm_start", lang, n=n),
            callback_data=f"queue:start:{project.id}",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            text=t("btn.back", lang),
            callback_data=f"project:{project.id}",
        )
    )
    return screen.View(text, kb)

def render_lang(user_id: int, lang: str, params: dict) -> screen.View:
    del user_id, params
    return screen.View(t("lang.choose", lang), language_keyboard())

def render_data(user_id: int, lang: str, params: dict) -> screen.View:
    del params
    n = user_data_mod.count_user_data(user_id)
    body = t("data.has", lang, n=n) if n else t("data.empty", lang)
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(
            t("btn.keys", lang), callback_data="keys:menu"
        ),
        types.InlineKeyboardButton(
            t("btn.autoreg", lang), callback_data="autoreg:menu"
        ),
    )
    if n:
        kb.row(
            types.InlineKeyboardButton(
                t("btn.data_edit", lang), callback_data="data:edit"
            ),
            types.InlineKeyboardButton(
                t("btn.delete_data", lang), callback_data="data:clear"
            ),
        )
    kb.add(types.InlineKeyboardButton(t("btn.back", lang), callback_data="projects:back"))
    return screen.View(t("data.title", lang) + "\n\n" + body, kb)


DATA_EDIT_PAGE = 8


def render_data_edit(
    user_id: int, lang: str, params: dict
) -> screen.View:
    """Row-by-row /data editor: paginated list, one 🗑 per row.

    The rows are the user's own file, in chat — the same identity the queue
    prepares jobs from (``load_user_data_rows``), so «строка 5» here is the
    row that disappears. Deletion is two-step, like data:clear.
    """
    rows = user_data_mod.load_user_data_rows(user_id)
    if not rows:
        return render_data(user_id, lang, params)
    try:
        page = max(1, int(params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    pages = max(1, (len(rows) + DATA_EDIT_PAGE - 1) // DATA_EDIT_PAGE)
    page = min(page, pages)
    start = (page - 1) * DATA_EDIT_PAGE
    chunk = rows[start : start + DATA_EDIT_PAGE]

    from bot.i18n import short_wallet

    body_lines = [
        t("data.edit_title", lang, page=page, pages=pages),
        "",
    ]
    kb = types.InlineKeyboardMarkup()
    for i, r in enumerate(chunk, start=start + 1):
        body_lines.append(
            f"{i}. {esc(r.handle or '—')} · <code>{short_wallet(r.wallet)}</code>"
        )
        kb.row(
            types.InlineKeyboardButton(
                t("data.del_row_btn", lang, n=i),
                callback_data=f"data:delask:{i}",
            )
        )
    nav: list[types.InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            types.InlineKeyboardButton("‹", callback_data=f"data:epage:{page - 1}")
        )
    if page < pages:
        nav.append(
            types.InlineKeyboardButton("›", callback_data=f"data:epage:{page + 1}")
        )
    if nav:
        kb.row(*nav)
    kb.row(
        types.InlineKeyboardButton(t("btn.back", lang), callback_data="data:menu")
    )
    return screen.View("\n".join(body_lines), kb)


def render_data_del_confirm(
    user_id: int, lang: str, params: dict
) -> screen.View:
    """Two-step confirm for deleting one row (mirrors data:clear_yes)."""
    rows = user_data_mod.load_user_data_rows(user_id)
    try:
        n = int(params.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1 or n > len(rows):
        return render_data_edit(user_id, lang, {})
    r = rows[n - 1]
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(
            t("btn.del_row_yes", lang), callback_data=f"data:delyes:{n}"
        )
    )
    kb.row(
        types.InlineKeyboardButton(
            t("btn.del_row_no", lang), callback_data="data:edit"
        )
    )
    return screen.View(
        t(
            "data.del_confirm",
            lang,
            n=n,
            handle=esc(r.handle or "—"),
            wallet=esc(r.wallet),
        ),
        kb,
    )

def render_proxy(user_id: int, lang: str, params: dict) -> screen.View:
    has_own = user_proxies_mod.has_own_proxies(user_id)
    text = proxy_status_text(user_id, lang)
    notice = str(params.get("notice") or "")
    if notice:
        text = f"{text}\n\n{notice}"
    return screen.View(text, proxy_keyboard(has_own=has_own, lang=lang))


def keys_keyboard(user_id: int, lang: str) -> types.InlineKeyboardMarkup:
    from bot import user_captcha

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Capsolver", callback_data="keys:prov:capsolver"),
        types.InlineKeyboardButton("2Captcha", callback_data="keys:prov:2captcha"),
    )
    kb.row(
        types.InlineKeyboardButton("Anti-Captcha", callback_data="keys:prov:anticaptcha")
    )
    # Multi-line paste: up to MAX_KEYS lines, one key per provider.
    kb.row(
        types.InlineKeyboardButton(
            t("btn.keys_multi", lang), callback_data="keys:multi"
        )
    )
    labels = {"capsolver": "Capsolver", "2captcha": "2Captcha",
              "anticaptcha": "Anti-Captcha"}
    for provider, key in user_captcha.get_keys(user_id):
        kb.row(
            types.InlineKeyboardButton(
                f"🗑 {labels.get(provider, provider)} …{key[-4:]}",
                callback_data=f"keys:del:{provider}",
            )
        )
    if user_captcha.has_key(user_id):
        kb.row(
            types.InlineKeyboardButton(
                t("btn.delete_key", lang), callback_data="keys:clear"
            )
        )
    kb.row(
        types.InlineKeyboardButton(t("btn.back", lang), callback_data="data:menu")
    )
    return kb


def autoreg_keyboard(user_id: int, lang: str) -> types.InlineKeyboardMarkup:
    from bot import db as bot_db

    on = bot_db.get_auto_reg(user_id)
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(
            t("btn.autoreg_off" if on else "btn.autoreg_on", lang),
            callback_data=f"autoreg:{'off' if on else 'on'}",
        )
    )
    kb.row(
        types.InlineKeyboardButton(
            t("btn.keys", lang), callback_data="keys:menu"
        )
    )
    kb.row(
        types.InlineKeyboardButton(t("btn.back", lang), callback_data="data:menu")
    )
    return kb


def render_autoreg(user_id: int, lang: str, params: dict) -> screen.View:
    from bot import db as bot_db

    del params
    on = bot_db.get_auto_reg(user_id)
    n = user_data_mod.count_user_data(user_id)
    body = t("autoreg.help", lang) + "\n\n"
    body += t("autoreg.state_on" if on else "autoreg.state_off", lang)
    if on and not n:
        body += "\n" + t("autoreg.data_need", lang)
    return screen.View(t("autoreg.title", lang) + "\n" + body,
                      autoreg_keyboard(user_id, lang))


def render_keys(user_id: int, lang: str, params: dict) -> screen.View:
    from bot import user_captcha

    rows = user_captcha.get_keys(user_id)
    body = t("keys.help_multi", lang) + "\n\n"
    if not rows:
        body += t("keys.none", lang)
    else:
        labels = {"capsolver": "Capsolver", "2captcha": "2Captcha",
                  "anticaptcha": "Anti-Captcha"}
        balance = str(params.get("balance") or "")
        lines = []
        for provider, key in rows:
            lines.append(
                t(
                    "keys.has",
                    lang,
                    masked=f"{labels.get(provider, provider)}: …{key[-4:]}",
                    balance=balance if balance else "—",
                )
            )
        body += "\n".join(lines)
    notice = str(params.get("notice") or "")
    if notice:
        body += f"\n\n{notice}"
    return screen.View(t("keys.title", lang) + "\n" + body, keys_keyboard(user_id, lang))

def render_suggest(user_id: int, lang: str, params: dict) -> screen.View:
    del user_id, lang, params
    return screen.View(suggest_prompt_text(), suggest_prompt_keyboard())


def render_plans(user_id: int, lang: str, params: dict) -> screen.View:
    """Old 💎 callback: holders still see status; everyone else the приватка."""
    del lang, params
    return render_privatka(user_id, privatka.LANG, {})


def render_privatka(user_id: int, lang: str, params: dict) -> screen.View:
    del lang, params
    try:
        bot_db.log_funnel_event(int(user_id), "priv_view", privatka.branch(user_id))
    except Exception:
        pass
    return screen.View(
        privatka.screen_text(user_id),
        privatka.screen_keyboard(user_id),
        disable_preview=True,
    )

def render_pay(user_id: int, lang: str, params: dict) -> screen.View:
    payment = bot_db.get_payment_record(str(params.get("payment_id") or ""))
    if payment is None or int(payment.get("user_id") or 0) != int(user_id):
        return render_plans(user_id, lang, {})
    if payment.get("credited_at"):
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                t("btn.to_projects", lang), callback_data="projects:back"
            )
        )
        return screen.View(billing_ux.paid_success_text(user_id, payment, lang), kb)
    return screen.View(
        billing_ux.payment_text(payment, lang),
        billing_ux.payment_keyboard(payment, lang),
    )

def render_queue(user_id: int, lang: str, params: dict) -> screen.View:
    """Queue dashboard. Progress redraws pass an explicit view; this is the
    fallback used by /queue, refreshes and restored anchors."""
    del params
    text, kb = queue_status_or_prompt(None, user_id=user_id, chat_id=0, lang=lang)
    return screen.View(text, kb)

BULK_ERRORS_PER_PAGE = 10

def _bulk_report_key(user_id: int) -> str:
    return f"bulk_report:{int(user_id)}"

def bulk_report_save(user_id: int, report: dict) -> None:
    """One report per user, overwritten each run and kept across restarts."""
    try:
        bot_db.set_setting(
            _bulk_report_key(user_id), json.dumps(report, ensure_ascii=False)
        )
    except Exception as exc:  # pragma: no cover - report is a nicety, not the job
        print(f"[bulk] report save failed: {exc}", flush=True)

def bulk_report_load(user_id: int) -> dict:
    raw = bot_db.get_setting(_bulk_report_key(user_id), "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def _bulk_summary_text(report: dict, lang: str) -> str:
    stats = report.get("stats") or {}
    text = t(
        "bulk.summary",
        lang,
        project=esc(str(report.get("project") or "")),
        ok=int(stats.get("OK", 0) or 0),
        already=int(stats.get("ALREADY", 0) or 0),
        bad_resp=int(stats.get("BAD_RESP", 0) or 0),
        fail=int(stats.get("FAIL", 0) or 0),
        busy=int(stats.get("BUSY", 0) or 0),
        format_bad=int(report.get("format_bad", 0) or 0),
        total_ok=int(report.get("total_ok", 0) or 0),
        limit=int(report.get("limit", 0) or 0),
        left=int(report.get("left", 0) or 0),
    )
    if int(stats.get("DEDUP", 0) or 0):
        text += t("bulk.dedup_note", lang, n=int(stats["DEDUP"]))
    if int(report.get("skipped_limit", 0) or 0):
        text += t("bulk.skipped_note", lang, n=int(report["skipped_limit"]))
    return text

def render_result(user_id: int, lang: str, params: dict) -> screen.View:
    """Bulk run: one live progress bar, then one summary card with paged errors."""
    phase = str(params.get("phase") or "done")
    if phase == "run":
        total = int(params.get("total") or 0)
        done = int(params.get("done") or 0)
        return screen.View(
            t(
                "bulk.progress",
                lang,
                project=esc(str(params.get("project") or "")),
                bar=progress_bar(done, total),
                done=done,
                total=total,
                ok=int(params.get("ok") or 0),
            ),
            None,
        )

    report = bulk_report_load(user_id)
    errors = [str(e) for e in (report.get("errors") or [])]
    if phase == "errors" and errors:
        pages = max(1, -(-len(errors) // BULK_ERRORS_PER_PAGE))
        try:
            page = int(params.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        page = max(0, min(page, pages - 1))
        start = page * BULK_ERRORS_PER_PAGE
        body = "\n".join(errors[start : start + BULK_ERRORS_PER_PAGE])
        kb = types.InlineKeyboardMarkup()
        if pages > 1:
            kb.row(
                types.InlineKeyboardButton(
                    t("btn.page_prev", lang),
                    callback_data=f"report:errors:{(page - 1) % pages}",
                ),
                types.InlineKeyboardButton(
                    t("btn.page_at", lang, page=page + 1, pages=pages),
                    callback_data="noop",
                ),
                types.InlineKeyboardButton(
                    t("btn.page_next", lang),
                    callback_data=f"report:errors:{(page + 1) % pages}",
                ),
            )
        kb.add(
            types.InlineKeyboardButton(
                t("btn.back_to_summary", lang), callback_data="report:summary"
            )
        )
        return screen.View(
            t("bulk.errors_title", lang, page=page + 1, pages=pages) + "\n\n" + body, kb
        )

    text = str(params.get("text") or "") or _bulk_summary_text(report, lang)
    kb = types.InlineKeyboardMarkup()
    if errors:
        kb.add(
            types.InlineKeyboardButton(
                t("btn.show_errors", lang, n=len(errors)),
                callback_data="report:errors:0",
            )
        )
    kb.add(
        types.InlineKeyboardButton(t("btn.to_projects", lang), callback_data="projects:back")
    )
    return screen.View(text, kb)

def register_screens() -> None:
    # Resolve bot.screen at call time: the test suite re-imports bot.* per
    # file, and the renderers must land in the module object in use now.
    from bot import screen as target

    target.register(target.STATE_HOME, render_home)
    target.register(target.STATE_GATE, render_gate)
    target.register(target.STATE_PROJECT, render_project)
    target.register(target.STATE_LANG, render_lang)
    target.register(target.STATE_DATA, render_data)
    target.register(target.STATE_DATA_EDIT, render_data_edit)
    target.register(target.STATE_DATA_DEL, render_data_del_confirm)
    target.register(target.STATE_PROXY, render_proxy)
    target.register(target.STATE_KEYS, render_keys)
    target.register(target.STATE_AUTOREG, render_autoreg)
    target.register(target.STATE_SUGGEST, render_suggest)
    target.register(target.STATE_RESULT, render_result)
    target.register(target.STATE_QUEUE, render_queue)
    target.register(target.STATE_PLANS, render_plans)
    target.register(target.STATE_PRIVATKA, render_privatka)
    target.register(target.STATE_PAY, render_pay)
