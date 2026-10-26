"""HTML text builders for Telegram admin panel."""

from __future__ import annotations

from typing import Any

from bot import config
from bot import db as bot_db
from bot import limits as bot_limits
from bot import project_health
from bot import proxy_pool
from bot import user_proxies as user_proxies_mod
from bot.ux import esc, safe_detail


def admin_home_text() -> str:
    off = bot_db.list_runtime_disabled_project_ids()
    off_line = (
        f"Runtime off: <b>{len(off)}</b> "
        f"({', '.join(esc(x) for x in off[:8])}{'…' if len(off) > 8 else ''})"
        if off
        else "Runtime off: —"
    )
    return (
        "<b>🛠 Админ</b>\n\n"
        "Статистика · Пользователи · Лимиты · Ошибки\n"
        "Проекты · Здоровье · Обновление · Рассылка\n"
        f"{off_line}\n"
        "Команды: /set_free_limit /set_vip_limit /get_limit"
    )


def format_runtime_projects_panel() -> str:
    """List runtime-disabled ids + short help."""
    off = bot_db.list_runtime_disabled_project_ids()
    lines = [
        "<b>📁 Projects (runtime)</b>",
        "Toml <code>enabled</code> не трогаем — только SQLite override.",
        "Form-closed алерт → кнопка «Выключить» → soft-cancel queue.",
        "",
        "<b>Runtime disabled:</b>",
    ]
    if off:
        for pid in off:
            lines.append(f"• <code>{esc(pid)}</code>")
    else:
        lines.append("• — (нет)")
    lines.append("")
    lines.append("Кнопки ниже: ▶ On (снять off) · ⏹ Off (live).")
    return "\n".join(lines)


def format_project_health_panel(
    projects: list[Any],
    health: dict[str, dict[str, Any]],
) -> str:
    """Compact cached project-health summary for Telegram admin UI."""
    lines = [
        "<b>🩺 Здоровье проектов</b>",
        "Read-only проверки · заявки не отправляются.",
        "",
    ]
    latest = ""
    for project in projects:
        row = health.get(project.id) or {}
        status = str(row.get("status") or "unknown")
        detail = safe_detail(str(row.get("detail") or "Не проверялся"), limit=90)
        checked = str(row.get("checked_at") or "")
        if checked > latest:
            latest = checked
        if bot_db.is_project_runtime_disabled(project.id):
            icon, label = "⏸", "скрыт"
        else:
            icon = project_health.status_icon(status)
            label = {
                "open": "регистрация открыта",
                "reachable": "сайт/API доступен",
                "config": "нужна настройка",
                "error": "ошибка проверки",
                "closed": "закрыт / endpoint удалён",
            }.get(status, "не проверялся")
        lines.append(f"{icon} <b>{esc(project.name)}</b> — {label}")
        if status in ("closed", "config", "error"):
            lines.append(f"   <code>{esc(detail)}</code>")
    if latest:
        lines.extend(["", f"Последняя проверка: <code>{esc(latest[:19])} UTC</code>"])
    lines.extend(
        [
            "",
            "🟢 открыта · ⚪ не проверена",
        ]
    )
    return "\n".join(lines)


def format_stats(
    snap: dict[str, Any],
    *,
    shared_proxies: int,
    own_proxy_users: int,
) -> str:
    free_l = bot_db.get_free_limit()
    vip_db = bot_db.list_user_limit_overrides()
    st24 = snap.get("by_status_24h") or {}
    stall = snap.get("by_status_all") or {}
    fun = snap.get("funnel") or {}
    lines = [
        "<b>📊 Stats</b>",
        f"Users: <b>{snap.get('users_total', 0)}</b> "
        f"(+{snap.get('users_24h', 0)} /24h) · banned: {snap.get('banned_n', 0)}",
        f"OK users: <b>{snap.get('ok_users', 0)}</b>",
        "",
        "<b>Submissions 24h</b>:",
        _status_line(st24),
        "<b>Submissions all</b>:",
        _status_line(stall),
        "",
        "<b>OK by project</b>:",
    ]
    okp = snap.get("ok_by_project") or []
    if okp:
        for pid, c in okp[:12]:
            lines.append(f"• <code>{esc(pid)}</code>: {c}")
    else:
        lines.append("• —")
    lines.extend(
        [
            "",
            "<b>Funnel (all time)</b>:",
            f"start <b>{fun.get('started', 0)}</b> → "
            f"selected <b>{fun.get('selected', 0)}</b> → "
            f"submit <b>{fun.get('submitted', 0)}</b> → "
            f"OK users <b>{fun.get('ok_users', 0)}</b>",
            "",
            f"Shared proxies: <b>{shared_proxies}</b>"
            + (
                " (pool OFF — own /proxy only)"
                if not proxy_pool.shared_enabled()
                else ""
            ),
            f"Users with own proxies: <b>{own_proxy_users}</b>",
            f"Limits: free=<b>{free_l}</b> "
            f"VIP_db={len(vip_db)} hardcode={len(config.PERMANENT_USER_OK_LIMITS)}",
        ]
    )
    return "\n".join(lines)


def _status_line(d: dict) -> str:
    if not d:
        return "• —"
    order = ("OK", "ALREADY", "BAD_RESP", "FAIL", "BUSY")
    parts = [f"{k}={d.get(k, 0)}" for k in order if k in d]
    for k, v in sorted(d.items()):
        if k not in order:
            parts.append(f"{k}={v}")
    return "• " + " · ".join(parts) if parts else "• —"


def format_user_card(user_id: int) -> str:
    row = bot_db.get_user_row(user_id)
    if not row:
        return f"User <code>{user_id}</code> не найден в БД."
    banned = int(row.get("banned") or 0) != 0
    mode = bot_limits.limit_mode(user_id)
    lim = bot_limits.effective_ok_limit(user_id)
    has_own = user_proxies_mod.has_own_proxies(user_id)
    un = row.get("username") or "—"
    fn = row.get("first_name") or "—"
    sel = row.get("selected_project") or "—"
    counts = bot_db.get_all_ok_counts(user_id)
    lines = [
        f"<b>👤 User</b> <code>{user_id}</code>",
        f"@{esc(un)} · {esc(fn)}",
        f"banned: <b>{'YES' if banned else 'no'}</b> · mode: <b>{mode}</b> · "
        f"limit: <b>{lim}</b> · own_proxies: {has_own}",
        f"selected: <code>{esc(sel)}</code>",
        "ok_count:",
    ]
    if counts:
        for pid, n in sorted(counts.items()):
            lines.append(f"• <code>{esc(pid)}</code>: {n}")
    else:
        lines.append("• —")
    lines.append("last submissions:")
    for s in bot_db.list_recent_submissions(user_id, 5):
        det = esc(safe_detail(s.get("detail") or "", limit=60))
        lines.append(
            f"• {esc(s.get('created_at', ''))[:19]} "
            f"<code>{esc(s.get('project_id'))}</code> "
            f"{esc(s.get('status'))} {det}"
        )
    return "\n".join(lines)


def format_limits_panel() -> str:
    free_l = bot_db.get_free_limit()
    lines = [
        "<b>🎚 Limits</b>",
        f"free_limit = <b>{free_l}</b>",
        "",
        "Hardcode VIP:",
    ]
    for uid, n in sorted(config.PERMANENT_USER_OK_LIMITS.items()):
        lines.append(f"• <code>{uid}</code> → {n}")
    lines.append("DB VIP overrides:")
    ov = bot_db.list_user_limit_overrides()
    if ov:
        for uid, n in ov:
            lines.append(f"• <code>{uid}</code> → {n}")
    else:
        lines.append("• (пусто)")
    lines.append("")
    lines.append(
        "Кнопки ниже или команды:\n"
        "<code>/set_free_limit N</code>\n"
        "<code>/set_vip_limit UID N</code>"
    )
    return "\n".join(lines)


def format_errors(rows: list[dict]) -> str:
    lines = ["<b>⚠️ Last errors</b> (FAIL/BAD_RESP/BUSY, без прокси-сбоев)", ""]
    if not rows:
        lines.append("Нет записей.")
        return "\n".join(lines)
    for r in rows:
        det = esc(safe_detail(r.get("detail") or "", limit=80))
        lines.append(
            f"• #{r.get('id')} u=<code>{r.get('user_id')}</code> "
            f"<code>{esc(r.get('project_id'))}</code> "
            f"<b>{esc(r.get('status'))}</b>\n"
            f"  {esc(str(r.get('created_at', ''))[:19])} {det}"
        )
    return "\n".join(lines)


def banned_user_text() -> str:
    return "⛔ Доступ ограничен. Обратись к администратору."
