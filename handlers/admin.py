"""Admin panel: commands and the admin:* callback tree.

Moved verbatim out of create_bot — same decorators, same indentation, bound
to the bot instance passed to register(). tgbot-module helpers are resolved
through the running module object (see handlers.current_tgbot).
"""

from __future__ import annotations

import json

import telebot


def register(bot: telebot.TeleBot, tg) -> None:
    from datetime import datetime, timezone
    from functools import partial
    from telebot import types

    from bot import admin_ux
    from bot import billing_ux
    from bot import config
    from bot import db as bot_db
    from bot import limits as bot_limits
    from bot import mint_ux
    from bot import mints as bot_mints
    from bot import x_client as bot_x_client
    from bot import payments_np
    from bot import broadcast as bot_broadcast
    from bot import new_project_alert as bot_new_project
    from bot import project_health as bot_project_health
    from bot import submit_queue as bot_queue
    from bot import user_proxies as user_proxies_mod
    from bot.i18n import t
    from bot import proxy_pool as bot_proxy_pool
    from bot.proxy_pool import get_proxies
    from bot.registry import get_project, list_projects
    from bot.ux import esc
    from bot.ui_screens import (
        admin_keyboard_back,
        admin_keyboard_batch,
        admin_keyboard_health,
        admin_keyboard_home,
        admin_keyboard_limits,
        admin_keyboard_payments,
        admin_keyboard_projects,
        admin_keyboard_reload,
        admin_keyboard_user,
    )
    import handlers.common as _common
    from handlers.state import (
        _broadcast_draft,
        _clear_user_modes,
        _get_admin_await,
        _set_admin_await,
        broadcast_audience,
    )

    _is_admin = _common.is_admin
    _admin_edit = partial(_common.admin_edit, bot)
    _reload_runtime = tg._reload_runtime
    _format_reload_result = tg._format_reload_result
    _admin_disable_project = tg._admin_disable_project
    _admin_enable_project = tg._admin_enable_project
    _notify_form_closed = tg._notify_form_closed
    count_own_proxy_users = tg.count_own_proxy_users
    run_mint_scan = tg.run_mint_scan
    _active_bot = tg._active_bot

    @bot.message_handler(commands=["admin"])
    def cmd_admin(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not _is_admin(u.id):
            bot.reply_to(message, t("no_access", bot_db.get_lang(u.id)))
            return
        _clear_user_modes(u.id)
        bot.reply_to(
            message,
            admin_ux.admin_home_text(),
            reply_markup=admin_keyboard_home(),
        )

    @bot.message_handler(commands=["funnel"])
    def cmd_funnel(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not _is_admin(u.id):
            bot.reply_to(message, t("no_access", bot_db.get_lang(u.id)))
            return
        try:
            text = bot_db.funnel_report()
        except Exception as exc:
            text = f"funnel: {esc(exc)}"
        bot.reply_to(message, text, parse_mode="HTML")

    @bot.message_handler(commands=["funnel_export"])
    def cmd_funnel_export(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not _is_admin(u.id):
            bot.reply_to(message, t("no_access", bot_db.get_lang(u.id)))
            return
        days = 30
        parts = (message.text or "").split()
        if len(parts) >= 2:
            try:
                days = int(parts[1])
            except ValueError:
                bot.reply_to(message, "Нужно число дней: /funnel_export 30")
                return
        if days > 90:
            bot.reply_to(message, "Максимум 90 дней. Укажи меньше: /funnel_export 30")
            return
        if days < 1:
            bot.reply_to(message, "Нужно от 1 до 90 дней: /funnel_export 30")
            return
        try:
            tsv = bot_db.funnel_export_tsv(days)
            bot.reply_to(message, f"<pre>{esc(tsv)}</pre>", parse_mode="HTML")
        except Exception as exc:
            bot.reply_to(message, f"funnel_export: {esc(exc)}")

    @bot.message_handler(commands=["gform"])
    def cmd_gform(message: types.Message) -> None:
        """Build a project from a public Google Form link (variant A, admin-only).

        /gform <viewform url> → parse → numbered fields → admin replies with
        N=source lines → a disabled projects/gf…/ folder is written. Enabling
        is the same two manual steps as any adapter: smoke, then enabled=true.
        """
        from bot import gform as bot_gform

        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not _is_admin(u.id):
            bot.reply_to(message, t("no_access", bot_db.get_lang(u.id)))
            return
        _clear_user_modes(u.id)
        raw = (message.text or "").split(maxsplit=1)
        url = raw[1].strip() if len(raw) > 1 else ""
        if not url:
            bot.reply_to(message, "Формат: <code>/gform https://docs.google.com/forms/d/e/…/viewform</code>",
                         parse_mode="HTML")
            return
        try:
            html = bot_gform.fetch_viewform(url)
            form = bot_gform.parse_form(url, html)
        except bot_gform.GformError as exc:
            bot.reply_to(message, f"ОТКАЗ: {esc(str(exc))}", parse_mode="HTML")
            return
        from handlers.state import _gform_draft

        _gform_draft[u.id] = json.dumps(form, ensure_ascii=False)
        _set_admin_await(u.id, "gform_map")
        bot.reply_to(message, bot_gform.render_fields(form), parse_mode="HTML")

    @bot.message_handler(commands=["mint"])
    def cmd_mint(message: types.Message) -> None:
        """Edit the calendar by hand. Admin only — it publishes to everyone."""
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not config.MINTS_ENABLED:
            bot.reply_to(message, "Календарь минтов выключен.")
            return
        if not _is_admin(u.id):
            bot.reply_to(message, t("no_access", bot_db.get_lang(u.id)))
            return
        raw = (message.text or "").split(maxsplit=1)
        body = raw[1].strip() if len(raw) > 1 else ""
        if not body:
            bot.reply_to(message, mint_ux.USAGE, parse_mode="HTML")
            return

        project_id, payload, errors = mint_ux.parse_mint_command(body)
        project = get_project(project_id) if project_id else None
        known = next((r for r in bot_mints.calendar({}) if r["project_id"] == project_id), None)
        display = (project.name if project else
                   payload.get("name") or (known or {}).get("project") or "")
        # `foo | clear` and `foo clear` both mean "take it off the calendar".
        # Checked before the name requirement: a clear needs no display name,
        # and for a calendar-only row project is None — that path used to
        # crash on project.name.
        if mint_ux.clear_target(body) is not None:
            bot_mints.clear_project(project_id)
            bot.reply_to(message, f"{esc(display or project_id)}: убран из календаря.",
                         parse_mode="HTML")
            return
        if project is None and not display:
            # A drop the bot does not register for can still belong on the
            # calendar — Hood Citizens mints on OpenSea with no form to submit.
            # It just needs a name, once.
            bot.reply_to(
                message,
                f"Проекта «{esc(project_id)}» нет в боте. Для календаря дайте имя:\n"
                f"<code>/mint {esc(project_id)} | name Hood Citizens | "
                f"public 18.08.2026 01:00 0.0005 ETH</code>",
                parse_mode="HTML")
            return
        if errors:
            # Nothing is written when anything failed to parse. A half-applied
            # edit is the shape that leaves a wrong date on screen next to a
            # right one.
            bot.reply_to(message, "Не применил:\n• " + "\n• ".join(esc(e) for e in errors),
                         parse_mode="HTML")
            return

        bot_mints.set_mint(project_id, supply=payload.get("supply"),
                           chain=payload.get("chain", ""), note=payload.get("note", ""),
                           mint_url=payload.get("mint_url", ""),
                           name=display, source="manual", updated_by=u.id)
        for phase in payload.get("phases", []):
            bot_mints.set_phase(project_id, phase["kind"],
                                starts_at=phase["starts_at"], price=phase.get("price", ""),
                                label=phase.get("label", ""))
        row = next((r for r in bot_mints.calendar(
            {project.id: project.name} if project else {})
            if r["project_id"] == project_id), None)
        summary = ", ".join(
            f"{p['label']} {mint_ux.when_text(p['starts_at'])}"
            for p in (row or {}).get("phases", [])
        ) or "без фаз"
        bot.reply_to(message,
                     f"✅ {esc(display)}: {esc(summary)}"
                     + (f" · саплай {row['supply']}" if row and row.get("supply") else ""),
                     parse_mode="HTML")

    @bot.message_handler(commands=["mints", "scan_mints"])
    def cmd_mints(message: types.Message) -> None:
        """Pending findings, and a way to force a scan without waiting 6 hours."""
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not config.MINTS_ENABLED:
            bot.reply_to(message, "Календарь минтов выключен.")
            return
        if not _is_admin(u.id):
            bot.reply_to(message, t("no_access", bot_db.get_lang(u.id)))
            return
        if (message.text or "").startswith("/scan_mints"):
            if not bot_x_client.enabled():
                bot.reply_to(message, "X_BEARER_TOKEN не задан — сканировать нечем.")
                return
            bot.reply_to(message, "Читаю X…")
            result = run_mint_scan(bot)
            found = len(result.get("proposals", []))
            unread = result.get("unread") or []
            lines = [f"Обошёл {result.get('projects', 0)} проектов, находок: {found}."]
            if unread:
                # Named, not buried: a scan that could not read six projects
                # and one that found nothing in them are different answers.
                lines.append(f"⚠️ Не удалось разобрать: {len(unread)} — "
                             f"{esc(', '.join(unread[:8]))}. Это не «пусто», "
                             f"это «не спросили». Повторите позже.")
            for line in (result.get("errors") or [])[:5]:
                lines.append(esc(line))
            bot.send_message(message.chat.id, "\n\n".join(lines), parse_mode="HTML")
            return

        pending = bot_mints.pending_proposals(limit=50)
        if not pending:
            bot.reply_to(message, "Неподтверждённых находок нет.")
            return
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("✅ Принять все", callback_data="mint:all"),
               types.InlineKeyboardButton("📋 По одной", callback_data="mint:each"))
        bot.reply_to(
            message,
            mint_ux.digest_text(pending, lambda pid: (get_project(pid).name
                                                      if get_project(pid) else pid)),
            parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)

    @bot.message_handler(commands=["reload", "reload_proxies", "reload_projects"])
    def cmd_reload(message: types.Message) -> None:
        """Hot-reload proxies + subscribers + projects (admin). Aliases kept."""
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        try:
            info = _reload_runtime(admin_id=message.from_user.id)
            bot.reply_to(message, _format_reload_result(info))
        except Exception as e:
            bot.reply_to(message, f"Ошибка: {esc(e)}")

    @bot.message_handler(commands=["set_free_limit"])
    def cmd_set_free_limit(message: types.Message) -> None:
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            bot.reply_to(message, "Использование: <code>/set_free_limit 10</code>")
            return
        ok, msg = bot_db.set_free_limit(int(parts[1]), admin_id=message.from_user.id)
        print(f"[admin] set_free_limit {parts[1]} by {message.from_user.id} ok={ok}", flush=True)
        bot.reply_to(message, ("✅ " if ok else "❌ ") + esc(msg))

    @bot.message_handler(commands=["limits_config"])
    def cmd_limits_config(message: types.Message) -> None:
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        free_l = bot_db.get_free_limit()
        ovrs = bot_db.list_user_limit_overrides()
        hard = ", ".join(
            f"{uid}={n}" for uid, n in sorted(config.PERMANENT_USER_OK_LIMITS.items())
        )
        lines = [
            "<b>Лимиты</b>",
            f"free_limit = <b>{free_l}</b>",
            f"hardcode VIP: <code>{esc(hard)}</code>",
            "DB VIP overrides:",
        ]
        if ovrs:
            for uid, n in ovrs:
                lines.append(f"• <code>{uid}</code> → {n}")
        else:
            lines.append("• (пусто)")
        bot.reply_to(message, "\n".join(lines))

    @bot.message_handler(commands=["set_vip_limit"])
    def cmd_set_vip_limit(message: types.Message) -> None:
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        parts = (message.text or "").split()
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
            bot.reply_to(
                message,
                "Использование: <code>/set_vip_limit USER_ID N</code>",
            )
            return
        ok, msg = bot_db.set_user_limit_override(
            int(parts[1]), int(parts[2]), admin_id=message.from_user.id
        )
        print(
            f"[admin] set_vip_limit {parts[1]}={parts[2]} by {message.from_user.id}",
            flush=True,
        )
        bot.reply_to(message, ("✅ " if ok else "❌ ") + esc(msg))

    @bot.message_handler(commands=["del_vip_limit"])
    def cmd_del_vip_limit(message: types.Message) -> None:
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            bot.reply_to(message, "Использование: <code>/del_vip_limit USER_ID</code>")
            return
        uid = int(parts[1])
        removed = bot_db.del_user_limit_override(uid)
        print(f"[admin] del_vip_limit {uid} by {message.from_user.id} removed={removed}", flush=True)
        if removed:
            hard = config.PERMANENT_USER_OK_LIMITS.get(uid)
            extra = f" hardcode={hard}" if hard is not None else " (не hardcode VIP)"
            bot.reply_to(message, f"✅ override снят для {uid}.{extra}")
        else:
            bot.reply_to(message, f"Нет DB override для {uid}.")

    @bot.message_handler(commands=["get_limit"])
    def cmd_get_limit(message: types.Message) -> None:
        if not _is_admin(message.from_user.id):
            bot.reply_to(message, "Нет доступа.")
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            bot.reply_to(message, "Использование: <code>/get_limit USER_ID</code>")
            return
        uid = int(parts[1])
        mode = bot_limits.limit_mode(uid)
        lim = bot_limits.effective_ok_limit(uid)
        has_own = user_proxies_mod.has_own_proxies(uid)
        counts = bot_db.get_all_ok_counts(uid)
        lines = [
            f"<b>User</b> <code>{uid}</code>",
            f"mode: <b>{mode}</b> · limit: <b>{lim}</b> · own_proxies: {has_own}",
            "ok_count по проектам:",
        ]
        if counts:
            for pid, n in sorted(counts.items()):
                lines.append(f"• {esc(pid)}: {n}")
        else:
            lines.append("• (нет)")
        bot.reply_to(message, "\n".join(lines))

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("gform:"))
    def on_gform_cb(call: types.CallbackQuery) -> None:
        """Publish-or-keep-private decision on a user's generated form."""
        from bot import gform as bot_gform
        from bot.registry import reload_projects

        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not _is_admin(u.id):
            try:
                bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            except Exception:
                pass
            return
        data = call.data or ""
        pid = data.rsplit(":", 1)[-1]
        try:
            d = bot_gform.set_public(pid, data.startswith("gform:pub:"))
        except bot_gform.GformError as exc:
            bot.answer_callback_query(call.id, str(exc)[:180], show_alert=True)
            return
        reload_projects()
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        verdict = ("✅ Проект опубликован — виден всем" if d.get("public")
                   else "✖️ Остался личным — видит только создатель")
        try:
            bot.edit_message_text(
                f"{verdict}\n<code>{esc(pid)}</code> · {esc(d.get('title') or '')}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass
        bot_db.log_admin_action(u.id, "gform_publish" if d.get("public") else "gform_keep_private",
                                detail=pid)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("admin:"))
    def on_admin_cb(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not _is_admin(u.id):
            try:
                bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            except Exception:
                pass
            return
        data = call.data or ""
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

        if data in ("admin:home", "admin:close"):
            _set_admin_await(u.id, None)
            if data == "admin:close":
                try:
                    bot.edit_message_text(
                        "Админ закрыт.",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                    )
                except Exception:
                    pass
                return
            _admin_edit(call, admin_ux.admin_home_text(), admin_keyboard_home())
            return

        if data == "admin:stats":
            snap = bot_db.admin_stats_snapshot()
            text = admin_ux.format_stats(
                snap,
                shared_proxies=len(get_proxies()),
                own_proxy_users=count_own_proxy_users(),
            )
            _admin_edit(call, text, admin_keyboard_back())
            return

        if data == "admin:user":
            _set_admin_await(u.id, "user_lookup")
            _admin_edit(
                call,
                "👤 Пришли <code>user_id</code> или <code>@username</code>",
                admin_keyboard_back(),
            )
            return

        if data == "admin:limits":
            _set_admin_await(u.id, None)
            _admin_edit(
                call, admin_ux.format_limits_panel(), admin_keyboard_limits()
            )
            return

        if data == "admin:subs":
            _set_admin_await(u.id, None)
            _admin_edit(
                call, billing_ux.format_subscriptions_panel(), admin_keyboard_back()
            )
            return

        if data in ("admin:pay", "admin:pay_min"):
            _set_admin_await(u.id, None)
            head = ""
            if data == "admin:pay_min":
                try:
                    value = payments_np.refresh_min_amount_cache(force=True)
                    head = f"Минимум обновлён: <b>${value}</b>\n\n"
                except Exception as exc:
                    head = f"Не удалось проверить минимум: <code>{esc(exc)}</code>\n\n"
            _admin_edit(
                call, head + billing_ux.format_payments_panel(), admin_keyboard_payments()
            )
            return

        if data == "admin:errors":
            rows = bot_db.list_recent_errors(20)
            _admin_edit(call, admin_ux.format_errors(rows), admin_keyboard_back())
            return

        if data == "admin:projects_noop":
            # The «стр. 2/3» label is a button because a keyboard row needs one.
            return

        if data == "admin:projects" or data.startswith("admin:projects:"):
            _set_admin_await(u.id, None)
            page = 0
            if data.startswith("admin:projects:"):
                try:
                    page = int(data.split(":", 2)[2])
                except (IndexError, ValueError):
                    page = 0
            _admin_edit(
                call,
                admin_ux.format_runtime_projects_panel(),
                admin_keyboard_projects(page),
            )
            return

        if data == "admin:npa_toggle":
            new_state = not bot_new_project.is_enabled()
            bot_new_project.set_enabled(new_state, admin_id=u.id)
            bot_db.log_admin_action(
                u.id, "new_project_alerts", detail=f"enabled={new_state}"
            )
            note = (
                "Новые проекты будут предлагаться к рассылке."
                if new_state
                else "Новые проекты будут молча помечаться как известные."
            )
            _admin_edit(
                call,
                f"{admin_ux.format_runtime_projects_panel()}\n\n🆕 Алерт: "
                f"<b>{'вкл' if new_state else 'выкл'}</b>. {esc(note)}",
                admin_keyboard_projects(),
            )
            return

        if data == "admin:sp_toggle":
            new_state = not bot_proxy_pool.shared_enabled()
            bot_proxy_pool.set_shared_enabled(new_state, admin_id=u.id)
            bot_db.log_admin_action(
                u.id, "shared_proxies", detail=f"enabled={new_state}"
            )
            extra = ""
            if not new_state:
                n_cancel = bot_queue.cancel_jobs_without_own_proxies(bot=bot)
                extra = f" Снято заданий без своих прокси: <b>{n_cancel}</b>."
            note = (
                "Общий пул снова раздаётся."
                if new_state
                else "Регистрации только через свои прокси."
            )
            _admin_edit(
                call,
                f"{admin_ux.format_runtime_projects_panel()}\n\n🌐 Пул: "
                f"<b>{'вкл' if bot_proxy_pool.shared_enabled() else 'выкл'}</b>. "
                f"{esc(note)}{extra}",
                admin_keyboard_projects(),
            )
            return

        if data in ("admin:health", "admin:health_refresh"):
            _set_admin_await(u.id, None)
            projects = list_projects(include_runtime_disabled=True)
            if data == "admin:health_refresh":
                try:
                    health = bot_project_health.refresh_projects(projects)
                    # Reuse the existing deduped alert path for explicit closed
                    # signals found by the read-only monitor.
                    for project in projects:
                        row = health.get(project.id) or {}
                        if (
                            row.get("status") == "closed"
                            and not bot_db.is_project_runtime_disabled(project.id)
                        ):
                            _notify_form_closed(
                                project,
                                "BAD_RESP",
                                str(row.get("detail") or "registration closed"),
                            )
                except Exception as e:
                    _admin_edit(
                        call,
                        f"❌ Health check: <code>{esc(e)}</code>",
                        admin_keyboard_health(),
                    )
                    return
            else:
                health = bot_project_health.cached_health_map(projects)
            _admin_edit(
                call,
                admin_ux.format_project_health_panel(projects, health),
                admin_keyboard_health(),
            )
            return

        if data.startswith("admin:proj_off:"):
            pid = data.split(":", 2)[-1].strip()
            text = _admin_disable_project(u.id, pid)
            _admin_edit(call, text, admin_keyboard_projects())
            return

        if data.startswith("admin:proj_on:"):
            pid = data.split(":", 2)[-1].strip()
            text = _admin_enable_project(u.id, pid)
            _admin_edit(call, text, admin_keyboard_projects())
            return

        if data in ("admin:reload", "admin:relall", "admin:relprox", "admin:relproj"):
            # Full reload only (proxies + subscribers + projects).
            try:
                info = _reload_runtime(admin_id=u.id)
                _admin_edit(
                    call,
                    _format_reload_result(info),
                    admin_keyboard_reload(),
                )
            except Exception as e:
                _admin_edit(
                    call,
                    f"❌ Reload error: <code>{esc(e)}</code>",
                    admin_keyboard_reload(),
                )
            return

        if data == "admin:setfree":
            _set_admin_await(u.id, "set_free")
            _admin_edit(call, "Число для free_limit:", admin_keyboard_back())
            return
        if data == "admin:setvip":
            _set_admin_await(u.id, "set_vip")
            _admin_edit(
                call,
                "Формат: <code>user_id N</code>",
                admin_keyboard_back(),
            )
            return

        if data == "admin:bcast":
            _set_admin_await(u.id, "bcast_text")
            _admin_edit(
                call,
                "📣 Пришли текст рассылки одним сообщением.\n"
                "Потом подтверждение.",
                admin_keyboard_back(),
            )
            return

        if data.startswith("admin:bcast_go:"):
            draft = _broadcast_draft.get(u.id, "")
            if not draft:
                _admin_edit(call, "Нет текста.", admin_keyboard_home())
                return
            audience = data.split(":", 2)[-1].strip()
            ids, label = broadcast_audience(audience)
            if not ids:
                _admin_edit(
                    call, f"Пусто: в «{label}» нет получателей.", admin_keyboard_home()
                )
                return
            bot_db.log_admin_action(
                u.id,
                "broadcast",
                detail=f"audience={audience} n={len(ids)} len={len(draft)}",
            )
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            batch = f"manual:{u.id}:{stamp}"
            # Plain text: avoid broken HTML / injection from a free-form draft.
            # interval_sec=0 — a manual broadcast keeps running at full speed;
            # only the paced new-project alert drips.
            queued = bot_broadcast.enqueue(batch, ids, body=draft, interval_sec=0.0)
            _broadcast_draft.pop(u.id, None)
            _set_admin_await(u.id, None)
            _admin_edit(
                call,
                f"📤 Рассылка «{esc(label)}» поставлена в очередь: "
                f"<b>{queued}</b> / {len(ids)}\n"
                "Доставка идёт в фоне и переживёт рестарт.",
                admin_keyboard_batch(batch),
            )
            return

        if data.startswith("admin:bstat:"):
            batch = data.split(":", 2)[-1].strip()
            _admin_edit(
                call,
                f"📤 <code>{esc(batch)}</code>\n{esc(bot_broadcast.format_stats(batch))}",
                admin_keyboard_batch(batch),
            )
            return

        if data.startswith("admin:bstop:"):
            batch = data.split(":", 2)[-1].strip()
            dropped = bot_broadcast.cancel(batch)
            bot_db.log_admin_action(
                u.id, "broadcast_cancel", detail=f"batch={batch} dropped={dropped}"
            )
            _admin_edit(
                call,
                f"🛑 Остановлено: снято из очереди <b>{dropped}</b>.\n"
                f"{esc(bot_broadcast.format_stats(batch))}",
                admin_keyboard_home(),
            )
            return

        if data.startswith("admin:npa:"):
            parts = data.split(":", 3)
            action = parts[2] if len(parts) > 3 else ""
            pid = parts[3].strip() if len(parts) > 3 else ""
            project = get_project(pid) if pid else None
            if action == "no":
                bot_db.log_admin_action(
                    u.id, "new_project_alert_skip", detail=f"project={pid}"
                )
                _admin_edit(
                    call,
                    f"✖️ <code>{esc(pid)}</code> — рассылать не буду. "
                    "Повторно предлагать не стану.",
                    admin_keyboard_home(),
                )
                return
            if action != "go":
                return
            if project is None:
                _admin_edit(
                    call,
                    f"Проект <code>{esc(pid)}</code> больше не в реестре.",
                    admin_keyboard_home(),
                )
                return
            batch, queued = bot_new_project.announce(project)
            minutes = (queued * bot_new_project.DEFAULT_INTERVAL_SEC) / 60
            bot_db.log_admin_action(
                u.id, "new_project_alert_send", detail=f"project={pid} n={queued}"
            )
            _admin_edit(
                call,
                f"📤 Анонс <b>{esc(project.name)}</b> поставлен в очередь: "
                f"<b>{queued}</b> получателей.\n"
                f"Темп 1 / {bot_new_project.DEFAULT_INTERVAL_SEC:g} с, "
                f"VIP первыми → ~{minutes:.0f} мин.",
                admin_keyboard_batch(batch),
            )
            return

        if data.startswith("admin:rst:"):
            parts = data.split(":")
            # admin:rst:UID or admin:rst:UID:yes
            if len(parts) < 3 or not parts[2].isdigit():
                return
            uid = int(parts[2])
            if len(parts) >= 4 and parts[3] == "yes":
                n = bot_db.reset_ok_count(uid, None, admin_id=u.id)
                _admin_edit(
                    call,
                    f"✅ Reset OK for <code>{uid}</code> (rows={n})\n\n"
                    + admin_ux.format_user_card(uid),
                    admin_keyboard_user(uid, bot_db.is_banned(uid)),
                )
            else:
                kb = types.InlineKeyboardMarkup()
                kb.add(
                    types.InlineKeyboardButton(
                        "Да, reset all",
                        callback_data=f"admin:rst:{uid}:yes",
                    )
                )
                kb.add(
                    types.InlineKeyboardButton(
                        "Отмена", callback_data=f"admin:u:{uid}"
                    )
                )
                _admin_edit(
                    call,
                    f"Сбросить ok_count для <code>{uid}</code> на всех проектах?",
                    kb,
                )
            return

        if data.startswith("admin:ban:"):
            parts = data.split(":")
            if len(parts) < 3 or not parts[2].isdigit():
                return
            uid = int(parts[2])
            if len(parts) >= 4 and parts[3] == "yes":
                bot_db.set_banned(uid, True, admin_id=u.id)
                bot_queue.cancel_jobs_for_user(
                    uid,
                    detail="user banned by admin",
                    bot=_active_bot,
                )
                _admin_edit(
                    call,
                    admin_ux.format_user_card(uid),
                    admin_keyboard_user(uid, True),
                )
            else:
                kb = types.InlineKeyboardMarkup()
                kb.add(
                    types.InlineKeyboardButton(
                        "Да, ban", callback_data=f"admin:ban:{uid}:yes"
                    )
                )
                kb.add(
                    types.InlineKeyboardButton(
                        "Отмена", callback_data=f"admin:u:{uid}"
                    )
                )
                _admin_edit(call, f"Забанить <code>{uid}</code>?", kb)
            return

        if data.startswith("admin:unban:"):
            parts = data.split(":")
            if len(parts) < 3 or not parts[2].isdigit():
                return
            uid = int(parts[2])
            bot_db.set_banned(uid, False, admin_id=u.id)
            _admin_edit(
                call,
                admin_ux.format_user_card(uid),
                admin_keyboard_user(uid, False),
            )
            return

        if data.startswith("admin:u:"):
            parts = data.split(":")
            if len(parts) >= 3 and parts[2].isdigit():
                uid = int(parts[2])
                _admin_edit(
                    call,
                    admin_ux.format_user_card(uid),
                    admin_keyboard_user(uid, bot_db.is_banned(uid)),
                )
            return
