"""Account profile callbacks: data:*, keys:*, autoreg:*, proxy:*.

Moved verbatim out of create_bot.
"""

from __future__ import annotations

import telebot


def register(bot: telebot.TeleBot, tg) -> None:
    from telebot import types

    from bot import db as bot_db
    from bot import limits as bot_limits
    from bot import screen
    from bot import user_captcha as user_captcha_mod
    from bot import user_data as user_data_mod
    from bot import user_proxies as user_proxies_mod
    from bot.i18n import t
    from handlers.state import (
        _clear_user_modes,
        _set_awaiting_data,
        _set_awaiting_key,
        _set_awaiting_proxy,
        _set_awaiting_suggest,
    )

    from functools import partial
    import handlers.common as _common
    _screen = partial(_common.show_screen, bot)
    _screen_gate = partial(_common.screen_gate, bot)
    _gate_sub = partial(_common.gate_sub, bot)
    _deny_if_banned = partial(_common.deny_if_banned, bot)
    _deny_if_banned_call = partial(_common.deny_if_banned_call, bot)
    _is_admin = _common.is_admin


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("data:"))
    def on_data_cb(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if bot_db.is_banned(u.id):
            try:
                bot.answer_callback_query(call.id, "Доступ ограничен", show_alert=True)
            except Exception:
                pass
            return
        allowed, deny_text, deny_kb = _gate_sub(u.id)
        if not allowed:
            try:
                bot.answer_callback_query(call.id, "Нужна подписка")
            except Exception:
                pass
            return
        data = call.data or ""
        lang = bot_db.get_lang(u.id)
        if data == "data:menu":
            _set_awaiting_proxy(u.id, False)
            _set_awaiting_suggest(u.id, False)
            _set_awaiting_data(u.id, True)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_DATA,
                lang=lang,
                call=call,
                toast=t("toast.data", lang),
            )
            return
        if data == "data:clear":
            # One tap on «Удалить данные» used to wipe the whole accounts file
            # immediately — the wallet list exists only on the user's side, so
            # an accidental tap lost it for good. Two steps, like proxy:clear.
            n = user_data_mod.count_user_data(u.id)
            kb = types.InlineKeyboardMarkup()
            kb.add(
                types.InlineKeyboardButton(
                    t("data.clear_yes", lang), callback_data="data:clear_yes"
                )
            )
            kb.add(
                types.InlineKeyboardButton(
                    t("btn.back", lang), callback_data="data:menu"
                )
            )
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_DATA,
                lang=lang,
                call=call,
                view=screen.View(t("data.confirm_clear", lang, n=n), kb),
            )
            return
        if data == "data:edit":
            _set_awaiting_data(u.id, True)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_DATA_EDIT,
                {"page": 1},
                lang=lang,
                call=call,
            )
            return
        if data.startswith("data:epage:"):
            try:
                page = max(1, int(data.rsplit(":", 1)[1]))
            except (IndexError, ValueError):
                page = 1
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_DATA_EDIT,
                {"page": page},
                lang=lang,
                call=call,
            )
            return
        if data.startswith("data:delask:"):
            try:
                n = int(data.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                n = 0
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_DATA_DEL,
                {"n": n},
                lang=lang,
                call=call,
            )
            return
        if data.startswith("data:delyes:"):
            try:
                n = int(data.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                n = 0
            ok = user_data_mod.delete_user_data_row(u.id, n)
            left = user_data_mod.count_user_data(u.id)
            _set_awaiting_data(u.id, True)
            state = screen.STATE_DATA_EDIT if (ok and left) else screen.STATE_DATA
            _screen(
                u.id,
                call.message.chat.id,
                state,
                {"page": 1},
                lang=lang,
                call=call,
                toast=(
                    t("toast.data_row_deleted", lang, n=n, n_left=left)
                    if ok
                    else t("toast.data_bad_row", lang)
                ),
            )
            return
        if data == "data:clear_yes":
            user_data_mod.clear_user_data(u.id)
            _set_awaiting_data(u.id, True)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_DATA,
                lang=lang,
                call=call,
                toast=t("data.deleted", lang),
            )
            return
        # Unknown/stale data:* value — still answer so the client stops spinning.
        screen.toast(bot, call)


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("keys:"))
    def on_keys_cb(call: types.CallbackQuery) -> None:
        """Own captcha key management: menu, provider pick, two-step delete."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        lang = bot_db.get_lang(u.id)
        data = call.data or ""

        if data == "keys:menu":
            _clear_user_modes(u.id)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_KEYS,
                lang=lang,
                call=call,
            )
            return

        if data.startswith("keys:prov:"):
            provider = user_captcha_mod.normalize_provider(
                data.split(":", 2)[-1]
            )
            if not provider:
                screen.toast(bot, call, "unknown provider", alert=True)
                return
            _set_awaiting_key(u.id, provider)
            label = {
                "capsolver": "Capsolver",
                "2captcha": "2Captcha",
                "anticaptcha": "Anti-Captcha",
            }[provider]
            screen.toast(bot, call)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_KEYS,
                {"notice": t("keys.send_prompt", lang, provider=label)},
                lang=lang,
            )
            return

        if data == "keys:multi":
            _set_awaiting_key(u.id, "multi")
            screen.toast(bot, call)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_KEYS,
                {"notice": t("keys.multi_prompt", lang)},
                lang=lang,
            )
            return

        if data.startswith("keys:del:"):
            provider = user_captcha_mod.normalize_provider(data.split(":", 2)[-1])
            if provider:
                user_captcha_mod.remove_key(u.id, provider)
                bot_limits.invalidate(u.id)
                label = {
                    "capsolver": "Capsolver",
                    "2captcha": "2Captcha",
                    "anticaptcha": "Anti-Captcha",
                }[provider]
                toast = t("keys.key_deleted", lang, provider=label)
            else:
                toast = "unknown provider"
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_KEYS,
                lang=lang,
                call=call,
                toast=toast,
            )
            return

        if data == "keys:clear":
            if not user_captcha_mod.has_key(u.id):
                _screen(u.id, call.message.chat.id, screen.STATE_KEYS, lang=lang, call=call)
                return
            kb = types.InlineKeyboardMarkup()
            kb.row(
                types.InlineKeyboardButton(
                    t("btn.cancel_yes", lang), callback_data="keys:clear_yes"
                ),
                types.InlineKeyboardButton(
                    t("btn.cancel_no", lang), callback_data="keys:menu"
                ),
            )
            screen.toast(bot, call)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_KEYS,
                {"notice": t("keys.confirm_clear", lang)},
                lang=lang,
                view=screen.View(t("keys.title", lang) + "\n\n" + t("keys.confirm_clear", lang), kb),
            )
            return

        if data == "keys:clear_yes":
            user_captcha_mod.clear_key(u.id)
            bot_limits.invalidate(u.id)
            screen.toast(bot, call, t("keys.cleared", lang))
            _screen(u.id, call.message.chat.id, screen.STATE_KEYS, lang=lang)
            return

        # Unknown/stale keys:* value — still answer so the client stops spinning.
        screen.toast(bot, call)


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("autoreg:"))
    def on_autoreg_cb(call: types.CallbackQuery) -> None:
        """Auto-registration toggle (users.auto_reg)."""
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        if _deny_if_banned_call(call):
            return
        lang = bot_db.get_lang(u.id)
        data = call.data or ""
        if data == "autoreg:menu":
            _screen(
                u.id, call.message.chat.id, screen.STATE_AUTOREG, lang=lang, call=call
            )
            return
        if data in ("autoreg:on", "autoreg:off"):
            on = data == "autoreg:on"
            bot_db.set_auto_reg(u.id, on)
            screen.toast(
                bot, call,
                t("autoreg.toggled_on" if on else "autoreg.toggled_off", lang),
            )
            _screen(u.id, call.message.chat.id, screen.STATE_AUTOREG, lang=lang)
            return
        # Unknown/stale autoreg:* value — still answer so the client stops spinning.
        screen.toast(bot, call)


    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("proxy:"))
    def on_proxy_cb(call: types.CallbackQuery) -> None:
        u = call.from_user
        bot_db.ensure_user(u.id, u.username, u.first_name)
        lang = bot_db.get_lang(u.id)
        data = call.data or ""
        # Ban blocks all proxy actions (menu, clear, clear_yes)
        if bot_db.is_banned(u.id):
            try:
                bot.answer_callback_query(call.id, "Доступ ограничен", show_alert=True)
            except Exception:
                pass
            return
        allowed, deny_text, _deny_kb = _gate_sub(u.id)
        if not allowed:
            _screen_gate(call, u.id, deny_text, toast=t("toast.need_sub", bot_db.get_lang(u.id)))
            return

        if data == "proxy:menu":
            _set_awaiting_suggest(u.id, False)
            _set_awaiting_data(u.id, False)
            _set_awaiting_proxy(u.id, True)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_PROXY,
                lang=lang,
                call=call,
                toast=t("toast.proxy", lang),
            )
            return

        if data == "proxy:check":
            # Live test of the user's OWN proxies: first 20 urls, one real
            # HTTPS request each, result lands on this screen as a notice.
            # The check itself runs in a background thread (bot/proxy_check).
            from bot import proxy_check

            urls = user_proxies_mod.load_user_proxy_urls(u.id)
            if not urls:
                try:
                    bot.answer_callback_query(
                        call.id, t("proxy.check_none", lang), show_alert=True
                    )
                except Exception:
                    pass
                return
            allowed, wait = proxy_check.may_run(u.id)
            if not allowed:
                try:
                    bot.answer_callback_query(
                        call.id,
                        t("proxy.check_cooldown", lang, sec=wait),
                        show_alert=True,
                    )
                except Exception:
                    pass
                return
            try:
                bot.answer_callback_query(
                    call.id, t("proxy.check_running", lang, n=min(len(urls), proxy_check.CHECK_LIMIT))
                )
            except Exception:
                pass
            chat_id = call.message.chat.id
            user_id = u.id

            def _show_report(report: dict) -> None:
                screen.show(
                    bot,
                    user_id,
                    chat_id,
                    screen.STATE_PROXY,
                    {
                        "notice": proxy_check.format_report(
                            report.get("results") or [],
                            report.get("total") or 0,
                            lang,
                        )
                    },
                    lang=lang,
                )

            proxy_check.start(user_id, urls, on_done=_show_report)
            return

        if data == "proxy:clear":
            # Two-step delete: confirm, then wipe. Deleting own proxies no
            # longer touches the limit (they are a traffic source only), so
            # the old "limit will drop" warning is gone with it.
            from bot.proxy_pool import shared_enabled as _shared_on

            warn = t(
                "proxy.confirm_clear_own_only"
                if not _shared_on()
                else "proxy.confirm_clear",
                lang,
            )
            kb = types.InlineKeyboardMarkup()
            kb.add(
                types.InlineKeyboardButton(
                    t("proxy.clear_yes", lang), callback_data="proxy:clear_yes"
                )
            )
            kb.add(
                types.InlineKeyboardButton(
                    t("btn.cancel_no", lang), callback_data="proxy:menu"
                )
            )
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_PROXY,
                lang=lang,
                call=call,
                view=screen.View(warn, kb),
            )
            return

        if data == "proxy:clear_yes":
            user_proxies_mod.clear_user_proxies(u.id)
            _set_awaiting_proxy(u.id, True)
            _screen(
                u.id,
                call.message.chat.id,
                screen.STATE_PROXY,
                {"notice": t("proxy.removed", lang)},
                lang=lang,
                call=call,
                toast=t("data.deleted", lang),
            )
            return
        # Unknown/stale proxy:* value — still answer so the client stops spinning.
        screen.toast(bot, call)
