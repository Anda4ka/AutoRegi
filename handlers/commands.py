"""User slash commands plus the small top-level callbacks.

Moved verbatim out of create_bot — same decorators, same indentation, bound
to the bot instance passed to register().
"""

from __future__ import annotations

import telebot


def register(bot: telebot.TeleBot, tg) -> None:
    from telebot import types

    from bot import admin_ux
    from bot import channel as bot_channel
    from bot import config
    from bot import db as bot_db
    from bot import group_tier as bot_group_tier
    from bot import limits as bot_limits
    from bot import mints as bot_mints
    from bot import mint_ux
    from bot import submit_queue as bot_queue
    from bot import user_data as user_data_mod
    from bot import user_proxies as user_proxies_mod
    from bot.i18n import t
    from bot.registry import get_project, list_projects
    from bot.ux import esc, remaining
    from bot.ui_screens import (
        language_keyboard,
        project_ready_text,
        project_screen_keyboard,
        projects_keyboard,
        proxy_keyboard,
        proxy_status_text,
        queue_status_or_autostart,
        status_text,
        subscribe_keyboard,
        subscribe_text,
        welcome_text,
    )
    from bot import screen
    from handlers.state import (
        _clear_user_modes,
        _set_awaiting_data,
        _set_awaiting_proxy,
        _set_awaiting_suggest,
    )
    notify_mint_proposal = tg.notify_mint_proposal

    from functools import partial
    import handlers.common as _common
    _screen = partial(_common.show_screen, bot)
    _screen_gate = partial(_common.screen_gate, bot)
    _gate_sub = partial(_common.gate_sub, bot)
    _deny_if_banned = partial(_common.deny_if_banned, bot)
    _deny_if_banned_call = partial(_common.deny_if_banned_call, bot)
    _is_admin = _common.is_admin


    @bot.message_handler(commands=["start", "projects"])
    def cmd_start(message: types.Message) -> None:
        u = message.from_user
        was_known = bot_db.is_known_user(u.id)
        bot_db.ensure_user(u.id, u.username, u.first_name)
        _clear_user_modes(u.id)
        if bot_db.is_banned(u.id):
            bot.reply_to(message, admin_ux.banned_user_text())
            return
        # First-visit auto-language: detect BEFORE the channel gate so the
        # subscription screen is also localized. Existing users keep their
        # stored choice (ru is the column default).
        lang = bot_db.get_lang(u.id)
        if was_known is False:
            lc = (getattr(u, "language_code", None) or "").lower()
            if lc.startswith("en"):
                bot_db.set_lang(u.id, "en")
                lang = "en"
        allowed, deny_text, deny_kb = _gate_sub(u.id, lang=lang)
        if not allowed:
            bot.reply_to(
                message, deny_text or subscribe_text(lang=lang), reply_markup=deny_kb
            )
            return
        projects = bot_limits.visible_projects(list_projects(), u.id)
        text = welcome_text(u.first_name, projects, user_id=u.id, lang=lang)
        bot.reply_to(
            message, text, reply_markup=projects_keyboard(lang, user_id=u.id)
        )


    @bot.message_handler(commands=["lang", "language"])
    def cmd_lang(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if bot_db.is_banned(u.id):
            bot.reply_to(message, admin_ux.banned_user_text())
            return
        bot.reply_to(
            message,
            t("lang.choose", bot_db.get_lang(u.id)),
            reply_markup=language_keyboard(),
        )


    @bot.message_handler(commands=["stats"])
    def cmd_stats(message: types.Message) -> None:
        """Per-user summary across all projects (not admin stats)."""
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if bot_db.is_banned(u.id):
            bot.reply_to(message, admin_ux.banned_user_text())
            return
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(message, deny_text or subscribe_text(), reply_markup=deny_kb)
            return
        lang = bot_db.get_lang(u.id)
        lim = bot_limits.effective_ok_limit(u.id)
        counts = bot_db.get_all_ok_counts(u.id)
        lines = [t("stats.title", lang), ""]
        for p in bot_limits.visible_projects(list_projects(), u.id):
            ok = counts.get(p.id, 0)
            left = remaining(ok, lim)
            lines.append(
                t(
                    "stats.line",
                    lang,
                    name=esc(p.name),
                    ok=ok,
                    limit=lim,
                    left=left,
                )
            )
        bot.reply_to(message, "\n".join(lines), disable_web_page_preview=True)


    @bot.message_handler(commands=["data", "accounts", "wallets"])
    def cmd_data(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned(message):
            return
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(message, deny_text or subscribe_text(), reply_markup=deny_kb)
            return
        lang = bot_db.get_lang(u.id)
        _set_awaiting_proxy(u.id, False)
        _set_awaiting_suggest(u.id, False)
        _set_awaiting_data(u.id, True)
        n = user_data_mod.count_user_data(u.id)
        body = (
            t("data.has", lang, n=n)
            if n
            else t("data.empty", lang)
        )
        kb = types.InlineKeyboardMarkup()
        if n:
            kb.add(
                types.InlineKeyboardButton(
                    t("btn.delete_data", lang), callback_data="data:clear"
                )
            )
        kb.add(
            types.InlineKeyboardButton(
                t("btn.back", lang), callback_data="projects:back"
            )
        )
        bot.reply_to(
            message,
            t("data.title", lang) + "\n\n" + body,
            reply_markup=kb,
            disable_web_page_preview=True,
        )


    @bot.message_handler(commands=["keys", "key", "captcha_key"])
    def cmd_keys(message: types.Message) -> None:
        """Own captcha solver key: upload/replace/delete (per-user, 0600)."""
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned(message):
            return
        lang = bot_db.get_lang(u.id)
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(
                message,
                deny_text or subscribe_text(lang=lang),
                reply_markup=deny_kb or subscribe_keyboard(lang=lang),
            )
            return
        _screen(u.id, message.chat.id, screen.STATE_KEYS, lang=lang)


    @bot.message_handler(commands=["priv", "privatka", "приватка"])
    def cmd_priv(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned(message):
            return
        lang = bot_db.get_lang(u.id)
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(
                message,
                deny_text or subscribe_text(lang=lang),
                reply_markup=deny_kb or subscribe_keyboard(lang=lang),
            )
            return
        from bot import privatka

        if not privatka.enabled():
            bot.reply_to(message, "—")
            return
        try:
            bot_db.log_funnel_event(u.id, "priv_cmd")
        except Exception:
            pass
        _screen(u.id, message.chat.id, screen.STATE_PRIVATKA, lang=lang)


    @bot.message_handler(commands=["autoreg", "auto_reg"])
    def cmd_autoreg(message: types.Message) -> None:
        """Auto-registration opt-in screen (new project → run my accounts)."""
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned(message):
            return
        lang = bot_db.get_lang(u.id)
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(
                message,
                deny_text or subscribe_text(lang=lang),
                reply_markup=deny_kb or subscribe_keyboard(lang=lang),
            )
            return
        _screen(u.id, message.chat.id, screen.STATE_AUTOREG, lang=lang)


    @bot.message_handler(commands=["queue"])
    def cmd_queue(message: types.Message) -> None:
        """Status of active job, or auto-start from /data + selected project."""
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned(message):
            return
        lang = bot_db.get_lang(u.id)
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(
                message,
                deny_text or subscribe_text(lang=lang),
                reply_markup=deny_kb or subscribe_keyboard(lang=lang),
            )
            return
        text, markup = queue_status_or_autostart(
            bot, user_id=u.id, chat_id=message.chat.id, lang=lang
        )
        bot.reply_to(
            message,
            text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )


    @bot.message_handler(commands=["cancel", "stop_queue", "qcancel"])
    def cmd_cancel(message: types.Message) -> None:
        """Cancel own queued/running job only."""
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned(message):
            return
        lang = bot_db.get_lang(u.id)
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(
                message,
                deny_text or subscribe_text(lang=lang),
                reply_markup=deny_kb or subscribe_keyboard(lang=lang),
            )
            return
        ok, reason, job = bot_queue.cancel_user_job(u.id)
        if not ok or job is None:
            if reason == "no active job":
                # No job — same as /queue: maybe auto-start if ready
                text, markup = queue_status_or_autostart(
                    bot, user_id=u.id, chat_id=message.chat.id, lang=lang
                )
                bot.reply_to(
                    message,
                    text,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            else:
                bot.reply_to(
                    message,
                    t("queue.cancel_fail", lang, reason=esc(reason or "?")),
                )
            return
        # The live screen carries the job status; the command only acks.
        bot_queue.update_job_message(bot, job, lang=lang)
        soft = reason == "cancelling" or (
            job.status == "running" and job.cancel_requested
        )
        bot.reply_to(
            message,
            t("queue.cancelling" if soft else "queue.cancelled", lang, id=job.id),
        )


    @bot.message_handler(commands=["proxy"])
    def cmd_proxy(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        lang = bot_db.get_lang(u.id)
        _set_awaiting_suggest(u.id, False)
        if _deny_if_banned(message):
            return
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(message, deny_text or subscribe_text(), reply_markup=deny_kb)
            return
        _set_awaiting_data(u.id, False)
        _set_awaiting_proxy(u.id, True)
        has_own = user_proxies_mod.has_own_proxies(u.id)
        bot.reply_to(
            message,
            proxy_status_text(u.id, lang=lang),
            reply_markup=proxy_keyboard(has_own=has_own, lang=lang),
            disable_web_page_preview=True,
        )


    @bot.message_handler(commands=["check", "sub", "subscribe"])
    def cmd_check(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        lang = bot_db.get_lang(u.id)
        if _deny_if_banned(message):
            return
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if allowed:
            lang = bot_db.get_lang(u.id)
            msg = (
                "✅ Subscription confirmed. You can register — /start"
                if lang == "en"
                else "✅ Подписка подтверждена. Можно регистрироваться — /start"
            )
            bot.reply_to(
                message,
                msg,
                reply_markup=projects_keyboard(lang, user_id=u.id),
            )
        else:
            bot.reply_to(message, deny_text or subscribe_text(), reply_markup=deny_kb)


    @bot.message_handler(commands=["help"])
    def cmd_help(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned(message):
            return
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(message, deny_text or subscribe_text(), reply_markup=deny_kb)
            return
        lang = bot_db.get_lang(u.id)
        pid = bot_db.get_selected_project(u.id)
        project = get_project(pid) if pid else None
        if project is None:
            bot.reply_to(
                message,
                t("need_project", lang),
                reply_markup=projects_keyboard(lang, user_id=u.id),
            )
            return
        ok = bot_db.get_ok_count(u.id, project.id)
        bot.reply_to(
            message,
            project_ready_text(project, ok, u.id, lang=lang),
            reply_markup=project_screen_keyboard(
                project, lang=lang, user_id=u.id
            ),
            disable_web_page_preview=True,
        )


    @bot.message_handler(commands=["limit", "status"])
    def cmd_limit(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned(message):
            return
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(message, deny_text or subscribe_text(), reply_markup=deny_kb)
            return
        bot.reply_to(message, status_text(u.id))


    @bot.callback_query_handler(func=lambda c: (c.data or "") == "subcheck")
    def on_subcheck(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        # This button *is* the user saying "I have joined now", so the web
        # gate's cached "no" — up to fifteen seconds old — has to go before the
        # check runs. Otherwise the chat would confirm the subscription while
        # the mini app kept refusing it, which reads as two bots disagreeing.
        bot_channel.invalidate_subscription(u.id)
        # Same button, same meaning for the group tier: someone who just
        # joined should not wait out a cached "not a member".
        bot_group_tier.invalidate(u.id)
        bot_limits.invalidate(u.id)
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if allowed:
            lang = bot_db.get_lang(u.id)
            notice = (
                "✅ Subscription confirmed."
                if lang == "en"
                else "✅ Подписка подтверждена."
            )
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_HOME,
                {"notice": notice},
                lang=lang,
                call=call,
                toast=t("toast.sub_ok", lang),
            )
        else:
            _screen_gate(
                call,
                u.id,
                deny_text,
                toast=t("toast.not_subscribed", bot_db.get_lang(u.id)),
            )


    @bot.callback_query_handler(func=lambda c: (c.data or "") == "suggest:start")
    def on_suggest_start(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if bot_db.is_banned(u.id):
            try:
                bot.answer_callback_query(call.id, "Доступ ограничен", show_alert=True)
            except Exception:
                pass
            return

        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return

        if bot_db.has_suggestion_today(u.id):
            # The daily cap is a toast, not a second message in the chat.
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_HOME,
                call=call,
                toast=t("toast.suggest_limit", bot_db.get_lang(u.id)),
                alert=True,
            )
            return

        _set_awaiting_suggest(u.id, True)
        bot_db.set_selected_project(u.id, None)
        _screen(
            u.id,
            call.message.chat.id,
            screen.STATE_SUGGEST,
            call=call,
            toast=t("toast.suggest_wait", bot_db.get_lang(u.id)),
        )


    @bot.callback_query_handler(func=lambda c: (c.data or "") == "suggest:cancel")
    def on_suggest_cancel(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        _set_awaiting_suggest(u.id, False)

        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text, toast="Отменено")
            return

        _screen(
            u.id,
            call.message.chat.id,
            screen.STATE_HOME,
            call=call,
            toast=t("toast.cancelled", bot_db.get_lang(u.id)),
        )


    @bot.callback_query_handler(func=lambda c: (c.data or "") in ("priv:menu", "priv:open"))
    def on_priv_menu(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        try:
            bot_db.log_funnel_event(u.id, "priv_menu", str(call.data or ""))
        except Exception:
            pass
        _screen(u.id, call.message.chat.id, screen.STATE_PRIVATKA, call=call)


    @bot.callback_query_handler(func=lambda c: (c.data or "") == "projects:back")
    def on_projects_back(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        bot_db.set_selected_project(u.id, None)
        _set_awaiting_suggest(u.id, False)
        _set_awaiting_proxy(u.id, False)

        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text, toast=t("toast.need_sub", bot_db.get_lang(u.id)))
            return

        lang = bot_db.get_lang(u.id)
        _screen(
            u.id,
            call.message.chat.id,
            screen.STATE_HOME,
            lang=lang,
            call=call,
            toast=t("toast.projects", lang),
        )


    @bot.callback_query_handler(
        func=lambda c: (c.data or "").startswith(("projects:page:", "projects:locked:"))
    )
    def on_projects_paging(call: types.CallbackQuery) -> None:
        """Page through the project list without leaving the live screen."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text, toast=t("toast.need_sub", bot_db.get_lang(u.id)))
            return
        data = call.data or ""
        _, kind, value = data.split(":", 2)
        params: dict = dict(screen.current_state(u.id)[1] or {})
        params.pop("notice", None)
        if kind == "page":
            params["page"] = int(value) if value.isdigit() else 0
        else:
            params["locked"] = value == "1"
            params["page"] = 0
        _screen(u.id, call.message.chat.id, screen.STATE_HOME, params, call=call)


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("mint:"))
    def on_mint_cb(call: types.CallbackQuery) -> None:
        """Accept, reject or edit one finding. Admin only."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if not config.MINTS_ENABLED:
            screen.toast(bot, call, "Календарь минтов выключен.", alert=True)
            return
        if not _is_admin(u.id):
            screen.toast(bot, call, t("no_access", bot_db.get_lang(u.id)), alert=True)
            return
        parts = (call.data or "").split(":")
        bulk = parts[1] if len(parts) == 2 else ""
        if bulk == "all":
            pending = bot_mints.pending_proposals(limit=50)
            applied = sum(1 for row in pending
                          if bot_mints.apply_proposal(row["id"], by=u.id))
            screen.toast(bot, call, f"Записано: {applied}")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=None)
                bot.send_message(call.message.chat.id,
                                 f"✅ В календарь записано находок: {applied}.")
            except Exception:  # noqa: BLE001
                pass
            return
        if bulk == "each":
            for row in bot_mints.pending_proposals(limit=20):
                notify_mint_proposal(bot, row)
            screen.toast(bot, call, "Отправил по одной")
            return

        action = parts[1] if len(parts) > 2 else ""
        try:
            proposal_id = int(parts[2])
        except (IndexError, ValueError):
            screen.toast(bot, call, "?", alert=True)
            return

        row = bot_mints.get_proposal(proposal_id)
        if row is None:
            # Already decided — most often from the copy of this message that
            # went to another admin. Say so instead of failing silently.
            screen.toast(bot, call, "Это предложение уже решено.", alert=True)
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            return

        project = get_project(row["project_id"])
        name = project.name if project else row["project_id"]

        if action == "ok":
            ok = bot_mints.apply_proposal(proposal_id, by=u.id)
            screen.toast(bot, call, "Опубликовано" if ok else "Не вышло")
            tail = "\n\n✅ <b>принято</b> — теперь видно в календаре"
        elif action == "no":
            bot_mints.decide_proposal(proposal_id, "rejected", by=u.id)
            screen.toast(bot, call, "Отклонено")
            tail = "\n\n✖️ <b>отклонено</b>"
        elif action == "ed":
            # No conversation state for this: a half-finished edit that expires
            # is worse than a command you can retype. The prefilled line below
            # is the proposal itself, so correcting it means changing one word.
            payload = row.get("payload") or {}
            bits = [row["project_id"]]
            if payload.get("supply"):
                bits.append(f"supply {payload['supply']}")
            if payload.get("chain"):
                bits.append(f"chain {payload['chain']}")
            for phase in (payload.get("phases") or []):
                when = phase.get("starts_at")
                shown = mint_ux.when_text(when).replace(" UTC", "") if when else "tba"
                bits.append(f"{phase['kind']} {shown} {phase.get('price') or ''}".strip())
            line = " | ".join(bits)
            bot.send_message(
                call.message.chat.id,
                f"Поправьте и отправьте:\n\n<code>/mint {mint_ux.esc(line)}</code>\n\n"
                "Предложение осталось в очереди — примите или отклоните его после правки.",
                parse_mode="HTML")
            screen.toast(bot, call, "Строка отправлена")
            return
        else:
            screen.toast(bot, call, "?", alert=True)
            return

        try:
            bot.edit_message_text(
                (call.message.html_text or call.message.text or f"{name}") + tail,
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="HTML", disable_web_page_preview=True, reply_markup=None)
        except Exception:  # noqa: BLE001 — the decision is already recorded
            pass


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("report:"))
    def on_report_cb(call: types.CallbackQuery) -> None:
        """Page the last bulk run's errors inside the same summary card."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        data = call.data or ""
        lang = bot_db.get_lang(u.id)
        if data == "report:summary":
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_RESULT,
                {"phase": "done"},
                lang=lang,
                call=call,
            )
            return
        page_raw = data.split(":")[-1]
        _screen(
            u.id,
            call.message.chat.id,
            screen.STATE_RESULT,
            {"phase": "errors", "page": int(page_raw) if page_raw.isdigit() else 0},
            lang=lang,
            call=call,
        )


    @bot.callback_query_handler(func=lambda c: (c.data or "") == "noop")
    def on_noop(call: types.CallbackQuery) -> None:
        """Page indicator: acknowledge the tap, change nothing."""
        screen.toast(bot, call)


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("lang:"))
    def on_lang_cb(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        data = call.data or ""
        action = data.split(":", 1)[-1].strip().lower()
        if action == "menu":
            _screen(u.id, call.message.chat.id, screen.STATE_LANG, call=call)
            return
        if action not in ("ru", "en"):
            screen.toast(bot, call, "?")
            return
        bot_db.set_lang(u.id, action)
        lang = action
        _screen(
            u.id,
            call.message.chat.id,
            screen.STATE_HOME,
            lang=lang,
            call=call,
            toast=t("lang.set_ru" if lang == "ru" else "lang.set_en", lang),
        )
