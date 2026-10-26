"""Project picker and the queue:* / qcancel:* callback tree.

Moved verbatim out of create_bot. Job start paths call tgbot's
start_project_queue_job / start_all_projects_batch through the running
module object so every tier gate stays in exactly one place.
"""

from __future__ import annotations

import telebot


def register(bot: telebot.TeleBot, tg) -> None:
    from telebot import types

    from bot import db as bot_db
    from bot import limits as bot_limits
    from bot import submit_queue as bot_queue
    from bot import screen
    from bot.i18n import t
    from bot.registry import get_project
    from handlers.state import (
        _set_awaiting_data,
        _set_awaiting_proxy,
        _set_awaiting_suggest,
    )
    start_project_queue_job = tg.start_project_queue_job
    start_all_projects_batch = tg.start_all_projects_batch

    from functools import partial
    import handlers.common as _common
    _screen = partial(_common.show_screen, bot)
    _screen_gate = partial(_common.screen_gate, bot)
    _gate_sub = partial(_common.gate_sub, bot)
    _deny_if_banned = partial(_common.deny_if_banned, bot)
    _deny_if_banned_call = partial(_common.deny_if_banned_call, bot)
    _is_admin = _common.is_admin


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("project:"))
    def on_project_pick(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return

        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return

        pid = (call.data or "").split(":", 1)[-1].strip()
        project = get_project(pid)
        # A tier-gated project is hidden from the list, but an old button in
        # the chat still carries its callback — re-check instead of trusting it.
        if project is None or not bot_limits.project_allowed(project, u.id):
            screen.toast(bot, call, t("toast.project_unavailable", bot_db.get_lang(u.id)))
            return

        _set_awaiting_suggest(u.id, False)
        _set_awaiting_proxy(u.id, False)
        _set_awaiting_data(u.id, False)
        bot_db.set_selected_project(u.id, project.id)
        _screen(
            u.id,
            call.message.chat.id,
            screen.STATE_PROJECT,
            {"pid": project.id},
            call=call,
            toast=t("toast.selected", bot_db.get_lang(u.id), name=project.name),
        )


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("results:"))
    def on_export_results(call: types.CallbackQuery) -> None:
        """«Мои регистрации»: the user's own OK/ALREADY rows as a .txt file.

        Read-only: no gates beyond ban/subscription can change what already
        happened, and the project may even be retired — the history is the
        user's to take with them."""
        import io as _io

        u = call.from_user
        if _deny_if_banned_call(call):
            return
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        pid = (call.data or "").split(":", 1)[-1].strip()
        lang = bot_db.get_lang(u.id)
        project = get_project(pid)
        name = project.name if project is not None else pid
        rows = bot_db.user_project_registrations(u.id, pid)
        if not rows:
            screen.toast(
                bot, call, t("toast.no_results", lang, name=name)
            )
            return
        lines = [f"# {name} — registrations ({len(rows)})"]
        lines.append("# handle\twallet\tstatus\tregistered_at")
        for r in rows:
            lines.append(
                "\t".join(
                    (
                        str(r.get("handle") or ""),
                        str(r.get("wallet") or ""),
                        str(r.get("status") or ""),
                        str(r.get("created_at") or "")[:19],
                    )
                )
            )
        doc = _io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))
        doc.name = f"{pid}_registrations.txt"
        try:
            bot.send_document(
                call.message.chat.id,
                doc,
                caption=t("export.caption", lang, name=name, n=len(rows)),
                disable_content_type_detection=True,
            )
            screen.toast(bot, call, t("export.sent", lang))
        except Exception:
            screen.toast(bot, call, t("export.error", lang))

    @bot.callback_query_handler(func=lambda c: (c.data or "") == "queue:status")
    def on_queue_status(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        lang = bot_db.get_lang(u.id)
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        _screen(u.id, call.message.chat.id, screen.STATE_QUEUE, lang=lang, call=call)


    @bot.callback_query_handler(
        func=lambda c: (c.data or "") == "queue:restart_data"
    )
    def on_queue_restart_data(call: types.CallbackQuery) -> None:
        """After bot restart: start job from /data + selected project."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        lang = bot_db.get_lang(u.id)
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        _screen(u.id, call.message.chat.id, screen.STATE_QUEUE, lang=lang, call=call)


    @bot.callback_query_handler(
        func=lambda c: (c.data or "").startswith("project_deliver:")
    )
    def on_project_deliver(call: types.CallbackQuery) -> None:
        """Re-send whatever artifact this project still holds for the user."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        lang = bot_db.get_lang(u.id)
        pid = (call.data or "").split(":", 1)[-1].strip()
        project = get_project(pid)
        hook = getattr(project, "queue_redeliver", None) if project else None
        if project is None or hook is None or not bot_limits.project_allowed(
            project, u.id
        ):
            screen.toast(bot, call, t("toast.project_unavailable", lang))
            return
        if bot_queue.user_has_project_job(u.id, project.id) is not None:
            # Handing the keys over erases the bot's copy, and a running job
            # still needs them to sign for the rows it has not reached yet.
            # Delivering now would strand those rows without a key: 226 of them
            # died that way (2026-08-13) before this guard existed. The job
            # delivers the batch itself when it ends, so waiting costs nothing.
            screen.toast(bot, call, t("toast.keys_busy", lang))
            return
        try:
            sent = int(hook(user_id=u.id, bot=bot) or 0)
        except Exception as exc:  # noqa: BLE001 — report, never break the screen
            # This used to call an undefined `log`, so the handler that was
            # meant to swallow a delivery failure raised NameError instead:
            # the toast below never ran, the tap died silently, and the real
            # exception was buried under the wrong one. Same shape as every
            # other report in this file.
            print(f"[deliver] {pid} user={u.id}: {exc}", flush=True)
            sent = 0
        screen.toast(
            bot, call, t("toast.keys_sent" if sent else "toast.keys_none", lang, n=sent)
        )


    @bot.callback_query_handler(
        func=lambda c: (c.data or "").startswith("queue:ask:")
    )
    def on_queue_ask_project(call: types.CallbackQuery) -> None:
        """Confirm screen for a self-sourced batch (one tap = a batch of slots)."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        lang = bot_db.get_lang(u.id)
        pid = (call.data or "").split(":", 2)[-1].strip()
        project = get_project(pid)
        if project is None or not bot_limits.project_allowed(project, u.id):
            screen.toast(bot, call, t("toast.project_unavailable", lang))
            return
        _screen(
            u.id,
            call.message.chat.id,
            screen.STATE_PROJECT,
            {"pid": project.id, "confirm": 1},
            call=call,
            lang=lang,
        )


    @bot.callback_query_handler(
        func=lambda c: (c.data or "").startswith("queue:start:")
    )
    def on_queue_start_project(call: types.CallbackQuery) -> None:
        """Explicit start: /data → queue for selected project (no second Yes)."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        lang = bot_db.get_lang(u.id)
        pid = (call.data or "").split(":", 2)[-1].strip()
        project = get_project(pid)
        if project is None:
            screen.toast(bot, call, t("toast.project_unavailable", lang))
            return
        bot_db.set_selected_project(u.id, project.id)
        screen.toast(bot, call, t("queue.starting", lang, name=project.name))
        ok_q, qmsg, job = start_project_queue_job(
            bot,
            user_id=u.id,
            chat_id=call.message.chat.id,
            project=project,
            lang=lang,
        )
        if not ok_q:
            # Why it could not start belongs on the screen the user is looking at.
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_QUEUE,
                lang=lang,
                view=screen.View(qmsg, None),
            )
        # Success is represented by the shared dashboard message created by
        # start_project_queue_job; do not send a second acknowledgement.
        # success: progress message already sent by start_project_queue_job
        del job


    @bot.callback_query_handler(func=lambda c: (c.data or "") == "queue:all")
    def on_queue_start_all(call: types.CallbackQuery) -> None:
        """VIP: queue every open project with free slots in one tap."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text)
            return
        lang = bot_db.get_lang(u.id)
        if not bot_limits.is_vip(u.id):
            screen.toast(bot, call, t("queue.batch_vip_only", lang))
            return
        # Preparing 20 projects' rows takes a moment — acknowledge first.
        screen.toast(bot, call, t("queue.batch_starting", lang))
        ok_b, msg_b, started = start_all_projects_batch(
            bot,
            user_id=u.id,
            chat_id=call.message.chat.id,
            lang=lang,
        )
        if not ok_b:
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_QUEUE,
                lang=lang,
                view=screen.View(msg_b, None),
            )
            return
        # Success already shows on the batch dashboard message.
        del started


    @bot.callback_query_handler(
        func=lambda c: (c.data or "").startswith("queue:cancel")
    )
    def on_queue_cancel(call: types.CallbackQuery) -> None:
        """Cancel own job from progress button (queue:cancel or queue:cancel:<id>)."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        lang = bot_db.get_lang(u.id)
        data = call.data or ""
        job_id = None
        parts = data.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            job_id = int(parts[2])

        ok, reason, job = bot_queue.cancel_user_job(u.id, job_id=job_id)
        if not ok or job is None:
            try:
                if reason == "no active job":
                    bot.answer_callback_query(
                        call.id, t("queue.none", lang)[:180], show_alert=True
                    )
                else:
                    bot.answer_callback_query(
                        call.id,
                        t("queue.cancel_fail", lang, reason=reason or "?")[:180],
                        show_alert=True,
                    )
            except Exception:
                pass
            return

        # update_job_message redraws the live screen; the outcome is a toast.
        soft = reason == "cancelling" or (
            job.status == "running" and job.cancel_requested
        )
        key = "queue.cancelling" if soft else "queue.cancelled"
        screen.toast(bot, call, t(key, lang, id=job.id))
        bot_queue.update_job_message(bot, job, lang=lang)


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("qcancel:"))
    def on_qcancel(call: types.CallbackQuery) -> None:
        """Two-step cancel: ask → confirm. Avoids accidental one-tap stop.

        Callback answered exactly once, after the result is known (so error
        alerts are actually shown instead of being swallowed).
        """
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        lang = bot_db.get_lang(u.id)
        data = call.data or ""
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        job_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

        def _answer(text: str = "", alert: bool = False) -> None:
            try:
                if text:
                    bot.answer_callback_query(call.id, text[:180], show_alert=alert)
                else:
                    bot.answer_callback_query(call.id)
            except Exception:
                pass

        # Batch stop: confirm once for the whole chain instead of per project.
        if action == "all":
            jobs = bot_queue.user_active_jobs(u.id)
            if not jobs:
                _answer(t("queue.none", lang), alert=True)
                return
            kb = bot_queue.cancel_all_confirm_keyboard(u.id, lang=lang)
            # The tap lands on the job dashboard, which is NOT the navigation
            # anchor (16.15): answer it and draw on the anchor instead of
            # re-anchoring onto the live progress message.
            _answer()
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_QUEUE,
                lang=lang,
                view=screen.View(
                    bot_queue.format_user_jobs_summary(u.id, lang=lang)
                    + "\n\n"
                    + t("queue.batch_cancel_confirm", lang, n=len(jobs)),
                    kb,
                ),
            )
            return

        if action == "allno":
            job = bot_queue.user_has_active_job(u.id)
            if job is not None:
                bot_queue.update_job_message(bot, job, lang=lang)
            _answer()
            return

        if action == "allyes":
            n, job = bot_queue.cancel_all_user_jobs(u.id)
            _answer(t("queue.batch_cancelled", lang, n=n))
            if job is not None:
                bot_queue.update_job_message(bot, job, lang=lang)
            return

        # Step 1: open the confirm keyboard for this exact job.
        if action == "ask":
            job = bot_queue.get_job(job_id) if job_id is not None else None
            if job is None or job.user_id != u.id:
                _answer(t("queue.cancel_fail", lang, reason="no active job"), alert=True)
                return
            kb = bot_queue.cancel_confirm_keyboard(job, lang=lang)
            if kb is None:
                _answer()
                return
            # Same as `all` above: never re-anchor onto the dashboard message.
            _answer()
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_QUEUE,
                {"job_id": job.id},
                lang=lang,
                view=screen.View(
                    bot_queue.format_job_status(job, lang=lang)
                    + "\n\n"
                    + t("queue.cancel_confirm", lang),
                    kb,
                ),
            )
            return

        # Step «no»: abort cancel, re-render THIS job's message (not whichever
        # running job user_has_active_job would prefer).
        if action == "no":
            job = bot_queue.get_job(job_id) if job_id is not None else None
            if job is None or job.user_id != u.id:
                _answer()
                return
            bot_queue.update_job_message(bot, job, lang=lang)
            _answer()
            return

        # Step 2: real cancel.
        if action == "yes":
            ok, reason, job = bot_queue.cancel_user_job(u.id, job_id=job_id)
            if not ok or job is None:
                _answer(
                    t("queue.cancel_fail", lang, reason=reason or "?"), alert=True
                )
                return
            soft = reason == "cancelling" or (
                job.status == "running" and job.cancel_requested
            )
            _answer(t("queue.cancelling" if soft else "queue.cancelled", lang, id=job.id))
            bot_queue.update_job_message(bot, job, lang=lang)
            return
