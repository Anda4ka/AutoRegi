"""Free-text handler: per-project submits, suggests, admin multi-step input.

Moved verbatim out of create_bot.
"""

from __future__ import annotations

import telebot


def register(bot: telebot.TeleBot, tg) -> None:
    from datetime import datetime, timezone
    from telebot import types

    from bot import admin_ux
    from bot import config
    from bot import db as bot_db
    from bot import errors as bot_errors
    from bot import limits as bot_limits
    from bot import rate_limit as bot_rate
    from bot import screen
    from bot import user_captcha as user_captcha_mod
    from bot.i18n import t
    from bot.registry import get_project
    from bot.ux import esc, remaining

    def _parse_key_line(line: str) -> tuple[str, str]:
        """``"provider key"`` / ``"provider:key"`` / ``"provider,key"`` →
        ``(provider, key)``. A bare ``CAP-…`` key is unambiguous capsolver;
        any other bare key is ambiguous between the two 32-hex providers and
        is refused with an empty provider.
        """
        import re as _re

        parts = [p.strip() for p in _re.split(r"[\s:,]+", line.strip(), maxsplit=1) if p.strip()]
        if not parts:
            return "", ""
        if len(parts) == 1:
            key = parts[0]
            if key.upper().startswith("CAP-"):
                return "capsolver", key
            return "", key
        provider = user_captcha_mod.normalize_provider(parts[0])
        return provider, parts[1] if provider else ""
    from bot.ui_screens import (
        SUGGEST_MAX_LEN,
        admin_keyboard_user,
        SUGGEST_MIN_LEN,
        default_result_message,
        projects_keyboard,
        subscribe_keyboard,
        subscribe_text,
        suggest_prompt_keyboard,
    )
    from handlers.state import (
        BROADCAST_AUDIENCES,
        _broadcast_draft,
        _get_admin_await,
        _get_awaiting_key,
        _is_awaiting_suggest,
        _set_admin_await,
        _set_awaiting_key,
        _set_awaiting_suggest,
        broadcast_audience,
    )
    from bot.ux import is_busy_detail, safe_edit_or_reply
    do_submit = tg.do_submit
    _user_lock = tg._user_lock
    _notify_form_closed = tg._notify_form_closed
    notify_admins_suggestion = tg.notify_admins_suggestion

    from functools import partial
    import json

    import handlers.common as _common
    _screen = partial(_common.show_screen, bot)
    _screen_gate = partial(_common.screen_gate, bot)
    _gate_sub = partial(_common.gate_sub, bot)
    _deny_if_banned = partial(_common.deny_if_banned, bot)
    _deny_if_banned_call = partial(_common.deny_if_banned_call, bot)
    _is_admin = _common.is_admin


    @bot.message_handler(func=lambda m: m.content_type == "text")
    def on_text(message: types.Message) -> None:
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)

        text = (message.text or "").strip()
        if text.startswith("/"):
            return

        # --- admin multi-step input ---
        admin_mode = _get_admin_await(u.id) if _is_admin(u.id) else None
        if admin_mode:
            if admin_mode == "user_lookup":
                uid = bot_db.find_user_id(text)
                _set_admin_await(u.id, None)
                if uid is None:
                    bot.reply_to(message, "Не найден. Пришли user_id или @username.")
                    return
                bot.reply_to(
                    message,
                    admin_ux.format_user_card(uid),
                    reply_markup=admin_keyboard_user(uid, bot_db.is_banned(uid)),
                    disable_web_page_preview=True,
                )
                return
            if admin_mode == "set_free":
                _set_admin_await(u.id, None)
                if not text.lstrip("-").isdigit():
                    bot.reply_to(message, "Нужно число.")
                    return
                ok, msg = bot_db.set_free_limit(int(text), admin_id=u.id)
                bot_db.log_admin_action(u.id, "set_free_limit", detail=text)
                bot.reply_to(message, ("✅ " if ok else "❌ ") + esc(msg))
                return
            if admin_mode == "set_vip":
                _set_admin_await(u.id, None)
                parts = text.split()
                if len(parts) < 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
                    bot.reply_to(message, "Формат: <code>user_id N</code>")
                    return
                ok, msg = bot_db.set_user_limit_override(
                    int(parts[0]), int(parts[1]), admin_id=u.id
                )
                bot.reply_to(message, ("✅ " if ok else "❌ ") + esc(msg))
                return
            if admin_mode == "gform_map":
                from bot import gform as bot_gform
                from handlers.state import _gform_draft

                _set_admin_await(u.id, None)
                raw_form = _gform_draft.pop(u.id, "")
                if not raw_form:
                    bot.reply_to(message, "Черновик устарел — начни заново: /gform &lt;ссылка&gt;")
                    return
                form = json.loads(raw_form)
                try:
                    mapping = bot_gform.parse_mapping(text, form)
                    pid, path = bot_gform.build_project(
                        form, mapping, u.id,
                        root=bot_gform.ROOT / "projects",
                        enabled=False, private=False,
                    )
                except bot_gform.GformError as exc:
                    bot.reply_to(message, "Не собрал:\n" + esc(str(exc)),
                                 parse_mode="HTML")
                    return
                bot.reply_to(
                    message,
                    f"✅ Черновик: <code>{esc(path.relative_to(bot_gform.ROOT))}</code> (enabled=false)\n"
                    f"Проба: <code>python tools/smoke_project.py {esc(pid)} --live</code>\n"
                    "Дальше: enabled = true → /reload",
                    parse_mode="HTML",
                )
                return
            if admin_mode == "bcast_text":
                _broadcast_draft[u.id] = text
                _set_admin_await(u.id, None)
                kb = types.InlineKeyboardMarkup()
                lines = [f"Черновик ({len(text)} символов). Кому отправить?"]
                for key in BROADCAST_AUDIENCES:
                    ids, label = broadcast_audience(key)
                    lines.append(f"• {label}: {len(ids)}")
                    kb.add(
                        types.InlineKeyboardButton(
                            f"{label} ({len(ids)})",
                            callback_data=f"admin:bcast_go:{key}",
                        )
                    )
                kb.add(
                    types.InlineKeyboardButton("Отмена", callback_data="admin:home")
                )
                bot.reply_to(message, "\n".join(lines), reply_markup=kb)
                return

        if bot_db.is_banned(u.id):
            bot.reply_to(message, admin_ux.banned_user_text())
            return

        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            _set_awaiting_suggest(u.id, False)
            _set_awaiting_key(u.id, None)
            bot.reply_to(
                message,
                deny_text or subscribe_text(),
                reply_markup=deny_kb or subscribe_keyboard(),
            )
            return

        # --- own captcha key upload ---
        awaiting_provider = _get_awaiting_key(u.id)
        if awaiting_provider:
            _set_awaiting_key(u.id, None)
            allowed_rate, rate_msg = bot_rate.allow(u.id, "key_upload", lang=lang)
            if not allowed_rate:
                bot.reply_to(message, rate_msg)
                return
            labels = {
                "capsolver": "Capsolver",
                "2captcha": "2Captcha",
                "anticaptcha": "Anti-Captcha",
            }

            if awaiting_provider == "multi":
                # Multi-key paste: one ``provider key`` line per key, ≤3 total,
                # one per provider (operator rule 2026-09-14).
                lines = [
                    ln.strip()
                    for ln in (text or "").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                if not lines:
                    bot.reply_to(message, t("keys.multi_line_hint", lang), parse_mode="HTML")
                    return
                saved: list[str] = []
                errors: list[str] = []
                for n, line in enumerate(lines, start=1):
                    provider, key = _parse_key_line(line)
                    if not provider or not key:
                        errors.append(
                            t("keys.multi_bad_line", lang, n=n, reason="")
                            + " "
                            + t("keys.multi_line_hint", lang)
                        )
                        continue
                    ok, _balance, detail = user_captcha_mod.validate_key(provider, key)
                    if not ok:
                        reason = (
                            t("keys.network_down", lang)
                            if "unreachable" in detail
                            else esc(detail)
                        )
                        errors.append(
                            t(
                                "keys.multi_bad_line",
                                lang,
                                n=n,
                                reason=f"{labels[provider]}: {reason}",
                            )
                        )
                        continue
                    ok_save, save_err = user_captcha_mod.save_key(u.id, provider, key)
                    if not ok_save:
                        errors.append(
                            t(
                                "keys.multi_bad_line",
                                lang,
                                n=n,
                                reason=f"{labels[provider]}: {esc(save_err)}",
                            )
                        )
                        continue
                    saved.append(labels[provider])
                if saved:
                    bot_limits.invalidate(u.id)
                    body = t("keys.multi_saved", lang, n=len(saved))
                    if errors:
                        body += "\n\n" + "\n".join(errors)
                    _screen(
                        u.id,
                        message.chat.id,
                        screen.STATE_KEYS,
                        lang=lang,
                        toast=body,
                    )
                    return
                bot.reply_to(
                    message,
                    "\n".join(errors) or t("keys.multi_line_hint", lang),
                    parse_mode="HTML",
                )
                return

            ok, balance, detail = user_captcha_mod.validate_key(
                awaiting_provider, text
            )
            if not ok:
                reason = detail if "unreachable" not in detail else t(
                    "keys.network_down", lang
                )
                bot.reply_to(
                    message,
                    t("keys.invalid", lang, reason=esc(reason)),
                    parse_mode="HTML",
                )
                return
            user_captcha_mod.save_key(u.id, awaiting_provider, text)
            bot_limits.invalidate(u.id)
            label = labels[awaiting_provider]
            _screen(
                u.id,
                message.chat.id,
                screen.STATE_KEYS,
                {"balance": f"{balance:.4f}".rstrip("0").rstrip(".")},
                lang=lang,
                toast=t("keys.saved", lang, provider=label, balance=f"{balance:g}"),
            )
            return

        # --- project suggestion flow ---
        if _is_awaiting_suggest(u.id):
            if len(text) < SUGGEST_MIN_LEN:
                bot.reply_to(
                    message,
                    f"Слишком коротко (мин. {SUGGEST_MIN_LEN} символов). "
                    f"Напиши название и ссылку.",
                    reply_markup=suggest_prompt_keyboard(),
                )
                return
            if len(text) > SUGGEST_MAX_LEN:
                bot.reply_to(
                    message,
                    f"Слишком длинно (макс. {SUGGEST_MAX_LEN} символов). Сократи текст.",
                    reply_markup=suggest_prompt_keyboard(),
                )
                return

            result = bot_db.add_suggestion(u.id, u.username, u.first_name, text)
            _set_awaiting_suggest(u.id, False)

            if not result.get("ok"):
                bot.reply_to(
                    message,
                    "⏳ Лимит: <b>1 предложение в сутки</b> (UTC).\n"
                    "Ты уже отправлял сегодня. Жди следующего дня (00:00 UTC).\n\n"
                    "/start — к проектам",
                    parse_mode="HTML",
                    reply_markup=projects_keyboard(user_id=u.id),
                )
                return

            n_admins = notify_admins_suggestion(
                bot,
                user_id=u.id,
                username=u.username,
                first_name=u.first_name,
                body=text,
                suggestion_id=int(result["id"]),
                utc_day=str(result["utc_day"]),
            )
            extra = ""
            if not config.ADMIN_IDS:
                extra = "\n⚠️ (Админы ещё не настроены в ADMIN_IDS — заявка сохранена в БД.)"
            elif n_admins == 0:
                extra = "\n⚠️ Не удалось доставить админам в ЛС (пусть напишут боту /start)."
            bot.reply_to(
                message,
                f"✅ Предложение <b>#{result['id']}</b> принято.\n"
                f"Следующее можно отправить после <b>00:00 UTC</b>.\n"
                f"{extra}\n\n"
                f"/start — к проектам",
                parse_mode="HTML",
                reply_markup=projects_keyboard(user_id=u.id),
            )
            return

        pid = bot_db.get_selected_project(u.id)
        project = get_project(pid) if pid else None
        if project is None:
            bot.reply_to(
                message,
                "Сначала выбери проект кнопкой ниже (или /start).\n"
                "Или нажми «💡 Предложить проект».",
                reply_markup=projects_keyboard(user_id=u.id),
            )
            return

        fields = project.parse(text)
        if fields is None:
            bot.reply_to(
                message,
                t(
                    "submit.bad_format",
                    bot_db.get_lang(u.id),
                    help=project.help_html(),
                ),
            )
            return

        err = project.validate(fields)
        if err:
            bot.reply_to(message, err)
            return

        ok_rl, rl_msg = bot_rate.allow(
            u.id,
            "submit",
            lang=bot_db.get_lang(u.id),
        )
        if not ok_rl:
            bot.reply_to(message, rl_msg)
            return

        lock = _user_lock(u.id)
        if not lock.acquire(blocking=False):
            bot.reply_to(
                message, t("busy_processing", bot_db.get_lang(u.id))
            )
            return

        try:
            # Re-check right before submit (user may have left the channel)
            allowed2, deny2, deny_kb2 = _gate_sub(u.id)
            if not allowed2:
                bot.reply_to(
                    message,
                    deny2 or subscribe_text(),
                    reply_markup=deny_kb2 or subscribe_keyboard(),
                )
                return

            ok_count = bot_db.get_ok_count(u.id, project.id)
            lim = bot_limits.effective_ok_limit(u.id, fresh=True)
            lang = bot_db.get_lang(u.id)
            if ok_count >= lim:
                extra = ""
                bot.reply_to(
                    message,
                    t(
                        "limit.exhausted",
                        lang,
                        name=esc(project.name),
                        limit=lim,
                        extra=extra,
                    ),
                    reply_markup=projects_keyboard(lang, user_id=u.id),
                )
                return

            wait = None
            handle_show = esc(fields.get("handle") or fields.get("twitter") or "?")
            wallet_pre = fields.get("wallet") or ""
            prev = bot_db.already_registered_wallet(project.id, wallet_pre)
            if prev:
                when = (prev.get("created_at") or "")[:10]
                safe_edit_or_reply(
                    bot,
                    message,
                    None,
                    t(
                        "dedup.skip",
                        lang,
                        name=esc(project.name),
                        wallet=esc(wallet_pre),
                        status=esc(prev.get("status") or "OK"),
                        when=esc(when or "—"),
                    ),
                )
                return

            claimed, creason = bot_db.try_claim_wallet(project.id, wallet_pre, u.id)
            if not claimed:
                safe_edit_or_reply(
                    bot,
                    message,
                    None,
                    t(
                        "dedup.skip",
                        lang,
                        name=esc(project.name),
                        wallet=esc(wallet_pre),
                        status=esc(
                            "OK" if creason == "already" else "IN_FLIGHT"
                        ),
                        when="—",
                    )
                    if creason == "already"
                    else (
                        f"⏳ Кошелёк уже отправляется (другой job). "
                        f"Подожди или /queue. <code>{esc(wallet_pre)}</code>"
                    ),
                )
                return

            slot_reservation = bot_db.try_reserve_ok_slot(
                u.id,
                project.id,
                lim,
            )
            if not slot_reservation:
                bot_db.release_wallet_claim(project.id, wallet_pre)
                bot.reply_to(
                    message,
                    t(
                        "limit.exhausted",
                        lang,
                        name=esc(project.name),
                        limit=lim,
                        extra="",
                    ),
                    reply_markup=projects_keyboard(lang, user_id=u.id),
                )
                return

            try:
                wait = bot.reply_to(
                    message,
                    t(
                        "submit.sending",
                        lang,
                        name=esc(project.name),
                        handle=handle_show,
                        left=remaining(ok_count, lim),
                        limit=lim,
                        retries=config.RETRIES,
                    ),
                )
            except Exception:
                wait = None

            try:
                result = do_submit(project, fields, user_id=u.id)
            except Exception as e:
                bot_db.release_wallet_claim(project.id, wallet_pre)
                bot_db.release_ok_slot(slot_reservation)
                # The traceback is operator data; the user gets a sentence and
                # a code that ties their report back to this log line.
                print(
                    f"[submit] {project.id} user={u.id} "
                    f"{bot_errors.error_code(str(e))}: {e}",
                    flush=True,
                )
                safe_edit_or_reply(
                    bot,
                    message,
                    wait,
                    bot_errors.user_message("FAIL", str(e), lang=lang),
                )
                return

            status = result.get("status", "FAIL")
            detail = result.get("detail", "")
            # Only promote real sheet-lock signals (is_busy_detail), not every
            # BAD_RESP/FAIL
            if status in ("BAD_RESP", "FAIL") and is_busy_detail(detail):
                status = "BUSY"
            handle = fields.get("handle") or fields.get("twitter") or result.get("handle")
            wallet = fields.get("wallet") or result.get("wallet") or wallet_pre
            extra = {
                k: v
                for k, v in fields.items()
                if k not in ("handle", "wallet", "twitter") and not k.startswith("_")
            }

            new_ok = bot_db.record_submission_and_finalize_claim(
                u.id,
                project.id,
                handle,
                wallet,
                status,
                detail,
                extra=extra or None,
                claim_wallet=wallet,
                slot_reservation_id=slot_reservation,
            )

            bot_db.append_bot_result(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "user_id": u.id,
                    "username": u.username,
                    "project_id": project.id,
                    "handle": handle,
                    "wallet": wallet,
                    "status": status,
                    "detail": (detail or "")[:300],
                    "ok_count": new_ok,
                    "bulk": False,
                }
            )
            _notify_form_closed(project, status, detail)

            lang = bot_db.get_lang(u.id)
            if project.format_result:
                try:
                    out = project.format_result(
                        result, fields, new_ok=new_ok, ok_limit=lim
                    )
                except TypeError:
                    out = project.format_result(result, fields)
                # Enrich OK project format_result with slots line if missing
                if status == "OK" and "осталось" not in out.lower() and "left" not in out.lower():
                    left = remaining(new_ok, lim)
                    out = out.rstrip() + "\n" + (
                        f"Осталось слотов: <b>{left}</b> из {lim}"
                        if lang == "ru"
                        else f"Slots left: <b>{left}</b> of {lim}"
                    )
            else:
                out = default_result_message(
                    project, status, detail, fields, new_ok, user_id=u.id, lang=lang
                )

            safe_edit_or_reply(bot, message, wait, out)
        finally:
            lock.release()
