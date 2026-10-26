"""Document uploads: /data, /proxy, per-project bulk files.

Moved verbatim out of create_bot, including the inner _record_and_log
(normalize status, write DB + JSONL). Submit paths go through tgbot's
do_submit / _user_lock so limits and the per-user lock stay singular.
"""

from __future__ import annotations

import telebot


def register(bot: telebot.TeleBot, tg) -> None:
    from datetime import datetime, timezone
    from telebot import types

    from bot import admin_ux
    from bot import bulk as bot_bulk
    from bot import config
    from bot import db as bot_db
    from bot import errors as bot_errors
    from bot import limits as bot_limits
    from bot import rate_limit as bot_rate
    from bot import screen
    from bot import user_data as user_data_mod
    from bot import user_proxies as user_proxies_mod
    from bot.i18n import t
    from bot.registry import get_project
    from bot.i18n import status_emoji
    from bot.ux import esc, remaining
    from bot.ui_screens import (
        bulk_report_save,
        projects_keyboard,
        proxy_keyboard,
        subscribe_keyboard,
        subscribe_text,
    )
    from handlers.state import (
        _is_awaiting_data,
        _is_awaiting_proxy,
        _set_awaiting_data,
        _set_awaiting_proxy,
        _set_awaiting_suggest,
    )
    from bot.ux import is_busy_detail
    do_submit = tg.do_submit
    _user_lock = tg._user_lock
    _notify_form_closed = tg._notify_form_closed

    def _record_and_log(
        *,
        user: types.User,
        project,
        fields: dict,
        result: dict,
        claim_wallet: str | None = None,
        slot_reservation_id: str | None = None,
    ) -> tuple[str, int]:
        """Normalize status, write DB + JSONL, return (status, new_ok)."""
        status = result.get("status", "FAIL")
        detail = result.get("detail", "")
        if status in ("BAD_RESP", "FAIL") and is_busy_detail(detail):
            status = "BUSY"
        handle = fields.get("handle") or fields.get("twitter") or result.get("handle")
        wallet = fields.get("wallet") or result.get("wallet")
        extra = {
            k: v
            for k, v in fields.items()
            if k not in ("handle", "wallet", "twitter") and not k.startswith("_")
        }
        try:
            new_ok = bot_db.record_submission_and_finalize_claim(
                user.id,
                project.id,
                handle,
                wallet,
                status,
                detail,
                extra=extra or None,
                claim_wallet=claim_wallet,
                slot_reservation_id=slot_reservation_id,
            )
        except Exception:
            # Same as the queue path: never strand a claim or slot reservation
            # because the record write itself failed.
            if claim_wallet:
                bot_db.release_wallet_claim(project.id, claim_wallet)
            if slot_reservation_id:
                bot_db.release_ok_slot(slot_reservation_id)
            raise
        bot_db.append_bot_result(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "user_id": user.id,
                "username": user.username,
                "project_id": project.id,
                "handle": handle,
                "wallet": wallet,
                "status": status,
                "detail": (detail or "")[:300],
                "ok_count": new_ok,
                "bulk": True,
            }
        )
        _notify_form_closed(project, status, detail)
        return status, new_ok

    from functools import partial
    import handlers.common as _common
    _screen = partial(_common.show_screen, bot)
    _screen_gate = partial(_common.screen_gate, bot)
    _gate_sub = partial(_common.gate_sub, bot)
    _deny_if_banned = partial(_common.deny_if_banned, bot)
    _deny_if_banned_call = partial(_common.deny_if_banned_call, bot)
    _is_admin = _common.is_admin


    @bot.message_handler(content_types=["document"])
    def on_document(message: types.Message) -> None:
        """Proxy .txt (awaiting_proxy) or data bulk .txt (project selected)."""
        u = message.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        lang = bot_db.get_lang(u.id)
        _set_awaiting_suggest(u.id, False)
        if bot_db.is_banned(u.id):
            bot.reply_to(message, admin_ux.banned_user_text())
            return

        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            bot.reply_to(
                message,
                deny_text or subscribe_text(),
                reply_markup=deny_kb or subscribe_keyboard(),
            )
            return

        doc = message.document
        if doc is None:
            return
        fname = (doc.file_name or "").strip()
        mime = (doc.mime_type or "").lower()
        looks_txt = fname.lower().endswith(".txt") or mime in (
            "text/plain",
            "application/octet-stream",
            "",
        )
        if not looks_txt:
            bot.reply_to(
                message,
                t("file.txt_required", lang, name=esc(fname or "—")),
            )
            return

        # --- proxy upload mode ---
        if _is_awaiting_proxy(u.id):
            ok_rl, rl_msg = bot_rate.allow(u.id, "proxy_file", lang=lang)
            if not ok_rl:
                bot.reply_to(message, rl_msg)
                return
            if doc.file_size and doc.file_size > config.USER_PROXY_MAX_BYTES:
                bot.reply_to(
                    message,
                    t(
                        "proxy.error.too_big",
                        lang,
                        kb=config.USER_PROXY_MAX_BYTES // 1024,
                    ),
                )
                return
            lock = _user_lock(u.id)
            if not lock.acquire(blocking=False):
                bot.reply_to(message, t("data.error.busy", lang))
                return
            try:
                try:
                    finfo = bot.get_file(doc.file_id)
                    raw = bot.download_file(finfo.file_path)
                except Exception as e:
                    bot.reply_to(
                        message,
                        t("data.error.download", lang, error=esc(e)),
                    )
                    return
                ok, msg, _n = user_proxies_mod.save_user_proxies_from_bytes(
                    u.id,
                    raw,
                    lang=lang,
                )
                if not ok:
                    bot.reply_to(
                        message,
                        f"❌ {msg}",
                        reply_markup=proxy_keyboard(
                            has_own=user_proxies_mod.has_own_proxies(u.id),
                            lang=lang,
                        ),
                    )
                    return
                urls = user_proxies_mod.load_user_proxy_urls(u.id)
                dok, dtried = user_proxies_mod.dns_sample_check(urls, sample=3)
                bot.reply_to(
                    message,
                    t(
                        "proxy.upload_done",
                        lang,
                        message=esc(msg),
                        dns_ok=dok,
                        dns_total=dtried,
                    ),
                    reply_markup=proxy_keyboard(has_own=True, lang=lang),
                )
            finally:
                lock.release()
            return

        # --- /data profile upload (handle|wallet) ---
        if _is_awaiting_data(u.id):
            ok_rl, rl_msg = bot_rate.allow(u.id, "bulk_file", lang=lang)
            if not ok_rl:
                bot.reply_to(message, rl_msg)
                return
            max_b = int(getattr(config, "USER_DATA_MAX_BYTES", 512 * 1024))
            if doc.file_size and doc.file_size > max_b:
                bot.reply_to(
                    message,
                    t("data.error.too_big", lang, kb=max_b // 1024),
                )
                return
            lock = _user_lock(u.id)
            if not lock.acquire(blocking=False):
                bot.reply_to(message, t("data.error.busy", lang))
                return
            try:
                try:
                    finfo = bot.get_file(doc.file_id)
                    raw = bot.download_file(finfo.file_path)
                except Exception as e:
                    bot.reply_to(
                        message,
                        t("data.error.download", lang, error=esc(e)),
                    )
                    return
                ok, msg, _n = user_data_mod.save_user_data_from_bytes(
                    u.id,
                    raw,
                    lang=lang,
                )
                if not ok:
                    bot.reply_to(message, f"❌ {msg}")
                    return
                _set_awaiting_data(u.id, False)
                bot.reply_to(
                    message,
                    t("data.upload_done", lang, message=esc(msg)),
                    reply_markup=projects_keyboard(lang, user_id=u.id),
                )
            finally:
                lock.release()
            return

        # --- data bulk ---
        ok_rl, rl_msg = bot_rate.allow(u.id, "bulk_file", lang=lang)
        if not ok_rl:
            bot.reply_to(message, rl_msg)
            return
        pid = bot_db.get_selected_project(u.id)
        project = get_project(pid) if pid else None
        if project is None:
            bot.reply_to(
                message,
                t("bulk.select_project", lang),
                reply_markup=projects_keyboard(lang, user_id=u.id),
            )
            return

        if not project.bulk_columns:
            bot.reply_to(
                message,
                t("bulk.not_configured", lang, project=esc(project.name)),
            )
            return

        bulk_max_lines = bot_limits.effective_bulk_max_lines(u.id)
        bulk_max_bytes = bot_limits.effective_bulk_max_bytes(u.id)

        if doc.file_size and doc.file_size > bulk_max_bytes:
            bot.reply_to(
                message,
                t("data.error.too_big", lang, kb=bulk_max_bytes // 1024),
            )
            return

        lock = _user_lock(u.id)
        if not lock.acquire(blocking=False):
            bot.reply_to(message, t("data.error.busy", lang))
            return

        try:
            try:
                finfo = bot.get_file(doc.file_id)
                raw = bot.download_file(finfo.file_path)
            except Exception as e:
                bot.reply_to(
                    message,
                    t("data.error.download", lang, error=esc(e)),
                )
                return

            text, dec_err = bot_bulk.decode_txt_bytes(
                raw,
                max_bytes=bulk_max_bytes,
                lang=lang,
            )
            if dec_err:
                bot.reply_to(message, f"❌ {dec_err}")
                return
            assert text is not None

            parsed = bot_bulk.parse_bulk_text(
                text,
                columns=project.bulk_columns,
                validate=project.validate,
                max_lines=bulk_max_lines,
                line_parse=getattr(project, "parse", None),
                allow_line_parse=bool(
                    getattr(project, "bulk_allow_line_parse", False)
                ),
                lang=lang,
            )
            if parsed.file_error:
                bot.reply_to(
                    message,
                    f"❌ {parsed.file_error}\n\n"
                    + bot_bulk.bulk_format_help(
                        project.bulk_columns,
                        project.name,
                        lang=lang,
                    ),
                )
                return

            n_valid = len(parsed.valid)
            n_bad = len(parsed.invalid)
            lim = bot_limits.effective_ok_limit(u.id, fresh=True)
            ok_count = bot_db.get_ok_count(u.id, project.id)
            slots_left = remaining(ok_count, lim)
            max_run = min(slots_left, bulk_max_lines)

            head = t(
                "bulk.header",
                lang,
                project=esc(project.name),
                valid=n_valid,
                bad=n_bad,
                slots=slots_left,
                limit=lim,
            )
            # Line errors are collected into the report and paged inside the
            # summary card instead of being chunked across several messages.
            parse_errors: list[str] = []
            if n_bad:
                parse_errors = [
                    line
                    for line in bot_bulk.format_line_errors(
                        parsed.invalid, lang=lang
                    ).splitlines()
                    if line.strip()
                ]

            def _report_card(text: str, errors: list[str]) -> None:
                bulk_report_save(
                    u.id,
                    {"project": project.name, "errors": errors, "stats": {}},
                )
                _screen(
                    u.id,
                    message.chat.id,
                    screen.STATE_RESULT,
                    {"phase": "done", "text": text},
                    lang=lang,
                )

            if n_valid == 0:
                _report_card(head + "\n\n" + t("bulk.nothing", lang), parse_errors)
                return

            if slots_left <= 0:
                _report_card(
                    head + "\n\n" + f"Лимит исчерпан: <b>{lim}</b> успешных.",
                    parse_errors,
                )
                return

            to_submit = parsed.valid[:max_run]
            skipped_limit = n_valid - len(to_submit)

            stats = {"OK": 0, "ALREADY": 0, "BAD_RESP": 0, "FAIL": 0, "BUSY": 0}
            fail_samples: list[str] = list(parse_errors)
            throttle = screen.Throttle(2.0)

            def _draw_progress(done: int, *, force: bool = False) -> None:
                if not throttle.ready(force=force):
                    return
                _screen(
                    u.id,
                    message.chat.id,
                    screen.STATE_RESULT,
                    {
                        "phase": "run",
                        "project": project.name,
                        "done": done,
                        "total": len(to_submit),
                        "ok": stats.get("OK", 0),
                    },
                    lang=lang,
                )

            _draw_progress(0, force=True)

            for idx, item in enumerate(to_submit, start=1):
                if bot_db.is_banned(u.id):
                    bot.reply_to(message, admin_ux.banned_user_text())
                    break
                fields = item.fields or {}
                cur_ok = bot_db.get_ok_count(u.id, project.id)
                if cur_ok >= lim:
                    skipped_limit += len(to_submit) - idx + 1
                    break
                prev = bot_db.already_registered_wallet(
                    project.id, fields.get("wallet")
                )
                if prev:
                    stats["ALREADY"] = stats.get("ALREADY", 0) + 1
                    stats["DEDUP"] = stats.get("DEDUP", 0) + 1
                    continue
                w_bulk = fields.get("wallet")
                claimed, creason = bot_db.try_claim_wallet(
                    project.id, w_bulk, u.id
                )
                if not claimed:
                    stats["ALREADY" if creason == "already" else "FAIL"] = (
                        stats.get(
                            "ALREADY" if creason == "already" else "FAIL", 0
                        )
                        + 1
                    )
                    if creason == "already":
                        stats["DEDUP"] = stats.get("DEDUP", 0) + 1
                    continue
                slot_reservation = bot_db.try_reserve_ok_slot(
                    u.id,
                    project.id,
                    lim,
                )
                if not slot_reservation:
                    bot_db.release_wallet_claim(project.id, w_bulk)
                    skipped_limit += len(to_submit) - idx + 1
                    break
                try:
                    result = do_submit(project, fields, user_id=u.id)
                except Exception as e:
                    result = {"status": "FAIL", "detail": str(e)}
                status, new_ok = _record_and_log(
                    user=u,
                    project=project,
                    fields=fields,
                    result=result,
                    claim_wallet=w_bulk,
                    slot_reservation_id=slot_reservation,
                )
                stats[status] = stats.get(status, 0) + 1
                if status not in ("OK", "ALREADY") and len(fail_samples) < 12:
                    h = esc(fields.get("handle") or "?")
                    fail_samples.append(
                        t(
                            "bulk.fail_line",
                            lang,
                            line=item.lineno,
                            handle=h,
                            status=status_emoji(status),
                            detail=bot_errors.user_message(
                                status, result.get("detail", ""), lang=lang
                            ),
                        )
                    )
                _draw_progress(idx, force=idx == len(to_submit))

            final_ok = bot_db.get_ok_count(u.id, project.id)
            bulk_report_save(
                u.id,
                {
                    "project": project.name,
                    "stats": stats,
                    "format_bad": n_bad,
                    "skipped_limit": skipped_limit,
                    "total_ok": final_ok,
                    "limit": lim,
                    "left": remaining(final_ok, lim),
                    "errors": fail_samples,
                },
            )
            _screen(
                u.id,
                message.chat.id,
                screen.STATE_RESULT,
                {"phase": "done"},
                lang=lang,
            )
        finally:
            lock.release()
