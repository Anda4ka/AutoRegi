"""RU/EN user-facing strings for the Telegram bot."""

from __future__ import annotations

from typing import Any

DEFAULT_LANG = "ru"
SUPPORTED = ("ru", "en")

# Flat key → {ru, en}. Use {name} style placeholders.
STRINGS: dict[str, dict[str, str]] = {
    # Language picker
    "lang.choose": {
        "ru": "Выбери язык / Choose language:",
        "en": "Choose language / Выбери язык:",
    },
    "lang.set_ru": {"ru": "Язык: Русский 🇷🇺", "en": "Language: Russian 🇷🇺"},
    "lang.set_en": {"ru": "Язык: English 🇬🇧", "en": "Language: English 🇬🇧"},
    "lang.btn_ru": {"ru": "🇷🇺 Русский", "en": "🇷🇺 Русский"},
    "lang.btn_en": {"ru": "🇬🇧 English", "en": "🇬🇧 English"},
    # Welcome / projects
    "welcome.hello": {
        "ru": "Привет{name}!",
        "en": "Hi{name}!",
    },
    "welcome.no_projects": {
        "ru": "Сейчас нет доступных проектов. Загляни позже.",
        "en": "No projects available right now. Check back later.",
    },
    "welcome.pick": {
        "ru": "Выбери проект:",
        "en": "Choose a project:",
    },
    "ops.own_proxy_only": {
        "ru": "⚠️ Общий пул прокси временно выключен. "
        "Регистрации идут только через свои прокси — загрузи их в «🌐 Прокси».",
        "en": "⚠️ The shared proxy pool is off for now. "
        "Registrations only go through your own proxies — upload them in Proxies.",
    },
    "welcome.summary": {
        "ru": "Всего OK: <b>{total}</b> · с ✅: <b>{badged}</b>/{projects}",
        "en": "Total OK: <b>{total}</b> · with ✅: <b>{badged}</b>/{projects}",
    },
    "welcome.limit_line": {
        "ru": "• <b>{name}</b> · ост. {left}",
        "en": "• <b>{name}</b> · left {left}",
    },
    "welcome.steps_header": {
        "ru": "Как пользоваться:",
        "en": "How it works:",
    },
    "welcome.step_data_done": {
        "ru": "✅ Аккаунты загружены: <b>{n}</b>",
        "en": "✅ Accounts loaded: <b>{n}</b>",
    },
    "welcome.step_data_need": {
        "ru": "⬜ Загрузите аккаунты (файл .txt) — кнопка ниже",
        "en": "⬜ Upload accounts (.txt file) — button below",
    },
    "welcome.step_pick_done": {
        "ru": "✅ Проект: {name}",
        "en": "✅ Project: {name}",
    },
    "welcome.step_pick": {
        "ru": "⬜ Выберите проект — кнопки ниже",
        "en": "⬜ Choose a project — buttons below",
    },
    "welcome.step_run": {
        "ru": "⬜ Запустите регистрацию на экране проекта",
        "en": "⬜ Start registration on the project screen",
    },
    "welcome.step_run_done": {
        "ru": "✅ Регистрация запущена — смотрите очередь",
        "en": "✅ Registration is running — see the queue",
    },
    "welcome.active_queue_none": {
        "ru": "Очередь: <b>пусто</b>",
        "en": "Queue: <b>none</b>",
    },
    "welcome.active_queue_run": {
        "ru": "Очередь: <b>{name}</b> — {done}/{total}",
        "en": "Queue: <b>{name}</b> — {done}/{total}",
    },
    "welcome.limit_info": {
        "ru": "Лимит: <b>{n}</b> {reg} на проект{tier}",
        "en": "Limit: <b>{n}</b> {reg} per project{tier}",
    },
    "welcome.accounts_compact": {
        "ru": "📦 Аккаунты: <b>{value}</b>",
        "en": "📦 Accounts: <b>{value}</b>",
    },
    "welcome.accounts_none": {"ru": "не загружены", "en": "not uploaded"},
    "welcome.project_compact": {
        "ru": "🎯 Проект: <b>{value}</b>",
        "en": "🎯 Project: <b>{value}</b>",
    },
    "welcome.project_none": {"ru": "не выбран", "en": "not selected"},
    "welcome.limit_compact": {
        "ru": "🎟 Лимит: <b>{n}</b> на проект{tier}",
        "en": "🎟 Limit: <b>{n}</b> per project{tier}",
    },
    "welcome.health_legend": {
        "ru": "🟢 открыта · ⚪ не проверена",
        "en": "🟢 open · ⚪ not checked",
    },
    "tier.free": {"ru": " (базовый)", "en": " (free)"},
    "tier.subscriber": {"ru": " (подписчик)", "en": " (subscriber)"},
    "tier.vip": {"ru": " (VIP)", "en": " (VIP)"},
    "tier.basic": {"ru": " (Basic)", "en": " (Basic)"},
    # Paid plans (bot/billing_ux.py)
    "plan.title": {"ru": "<b>💎 Тарифы</b>", "en": "<b>💎 Plans</b>"},
    "plan.current_free": {
        "ru": "Сейчас: <b>Free</b> · {n} регистраций на проект · общий пул прокси",
        "en": "Now: <b>Free</b> · {n} registrations per project · shared proxy pool",
    },
    "plan.current_paid": {
        "ru": "Сейчас: {emoji} <b>{name}</b> до {until} ({days} дн.) · {n} на проект",
        "en": "Now: {emoji} <b>{name}</b> until {until} ({days} d) · {n} per project",
    },
    "plan.card": {
        "ru": "{emoji} <b>{name}</b> — ${price} / {days} дней\n   {limit} регистраций на каждый проект",
        "en": "{emoji} <b>{name}</b> — ${price} / {days} days\n   {limit} registrations per project",
    },
    "plan.unavailable": {
        "ru": "   ⚠️ временно недоступен к оплате",
        "en": "   ⚠️ temporarily not purchasable",
    },
    "plan.sales_closed_free": {
        "ru": "🔒 Продажа тарифов закрыта — новые подписки не оформляются.",
        "en": "🔒 Plan sales are closed — no new subscriptions are issued.",
    },
    "plan.sales_closed_paid": {
        "ru": "🔒 Продажа тарифов закрыта. Твой тариф работает до конца оплаченного срока.",
        "en": "🔒 Plan sales are closed. Your plan keeps running until it expires.",
    },
    "plan.network_note": {
        "ru": "Оплата: <b>USDT</b> в сети <b>BNB Smart Chain (BEP-20)</b>.",
        "en": "Payment: <b>USDT</b> on <b>BNB Smart Chain (BEP-20)</b>.",
    },
    "plan.granted": {
        "ru": "{emoji} Тариф <b>{name}</b> активен до {until}.",
        "en": "{emoji} Plan <b>{name}</b> is active until {until}.",
    },
    "plan.expiring": {
        "ru": "⏳ {emoji} <b>{name}</b> заканчивается через {days} дн. ({until}).\nПродлить: 💎 Тарифы",
        "en": "⏳ {emoji} <b>{name}</b> ends in {days} d ({until}).\nRenew: 💎 Plans",
    },
    "plan.expired": {
        "ru": "Тариф <b>{name}</b> закончился — снова {n} регистраций на проект.\nПродлить: 💎 Тарифы",
        "en": "Plan <b>{name}</b> has ended — back to {n} registrations per project.\nRenew: 💎 Plans",
    },
    # Same two notices with the renewal call to action removed: pointing a user
    # at a shop that is shut is worse than saying nothing.
    "plan.expiring_closed": {
        "ru": "⏳ {emoji} <b>{name}</b> заканчивается через {days} дн. ({until}).\nПродажа тарифов закрыта — дальше будет базовый лимит.",
        "en": "⏳ {emoji} <b>{name}</b> ends in {days} d ({until}).\nPlan sales are closed — the free limit applies afterwards.",
    },
    "plan.expired_closed": {
        "ru": "Тариф <b>{name}</b> закончился — снова {n} регистраций на проект.\nПродажа тарифов закрыта.",
        "en": "Plan <b>{name}</b> has ended — back to {n} registrations per project.\nPlan sales are closed.",
    },
    "btn.plans": {"ru": "💎 Тарифы", "en": "💎 Plans"},
    # Private-group upsell: always shown in Russian. Generic placeholder copy —
    # edit these to describe your own paid group before turning the feature on.
    "priv.btn_nav": {"ru": "⭐ Приватка", "en": "⭐ Приватка"},
    "priv.btn_win": {"ru": "⭐ Приватка — подписка", "en": "⭐ Приватка — подписка"},
    "priv.btn_pay": {
        "ru": "Оплатить в @{bot}",
        "en": "Оплатить в @{bot}",
    },
    "priv.btn_manage": {
        "ru": "Подписка в @{bot}",
        "en": "Подписка в @{bot}",
    },
    "priv.win_line": {
        "ru": "5/5 готово ✅ С приваткой лимит выше — подробности в @{bot}",
        "en": "5/5 готово ✅ С приваткой лимит выше — подробности в @{bot}",
    },
    "priv.basic_line": {
        "ru": "Твой Basic до {date}. Дальше — приватка: подробности в @{bot}.",
        "en": "Твой Basic до {date}. Дальше — приватка: подробности в @{bot}.",
    },
    "priv.screen_offer": {
        "ru": (
            "<b>Приватка</b>\n\n"
            "Платная подписка с повышенными лимитами.\n\n"
            "Оплата только в @{bot}."
        ),
        "en": (
            "<b>Приватка</b>\n\n"
            "Платная подписка с повышенными лимитами.\n\n"
            "Оплата только в @{bot}."
        ),
    },
    "priv.screen_file": {
        "ru": (
            "<b>Приватка</b>\n\n"
            "Платная подписка с повышенными лимитами.\n\n"
            "Оплата только в @{bot}."
        ),
        "en": (
            "<b>Приватка</b>\n\n"
            "Платная подписка с повышенными лимитами.\n\n"
            "Оплата только в @{bot}."
        ),
    },
    "priv.screen_group": {
        "ru": "У тебя уже активна приватка ✅",
        "en": "У тебя уже активна приватка ✅",
    },
    "priv.screen_vip": {
        "ru": "VIP-доступ активен ✅",
        "en": "VIP-доступ активен ✅",
    },
    "btn.renew": {"ru": "💎 Продлить", "en": "💎 Renew"},
    "btn.my_plan": {"ru": "💎 Мой тариф", "en": "💎 My plan"},
    "btn.buy_plan": {"ru": "{emoji} {name} — ${price}", "en": "{emoji} {name} — ${price}"},
    "btn.pay_check": {"ru": "🔄 Проверить оплату", "en": "🔄 Check payment"},
    "btn.pay_cancel": {"ru": "✖️ Отменить", "en": "✖️ Cancel"},
    "btn.pay_retry": {"ru": "🔁 Создать новый платёж", "en": "🔁 New payment"},
    # Payment screen
    "pay.title": {
        "ru": "<b>💳 Оплата</b> · {emoji} {name} · {days} дней",
        "en": "<b>💳 Payment</b> · {emoji} {name} · {days} days",
    },
    "pay.amount": {"ru": "Сумма: <b>{amount} USDT</b>", "en": "Amount: <b>{amount} USDT</b>"},
    "pay.network": {
        "ru": "Сеть: <b>BNB Smart Chain (BEP-20)</b> ⚠️ только эта сеть",
        "en": "Network: <b>BNB Smart Chain (BEP-20)</b> ⚠️ this network only",
    },
    "pay.address": {"ru": "Адрес (нажми, чтобы скопировать):", "en": "Address (tap to copy):"},
    "pay.valid_days": {"ru": "⏳ Действителен ещё {n} дн.", "en": "⏳ Valid for {n} more days"},
    "pay.valid_hours": {"ru": "⏳ Действителен ещё {h} ч {m} мин", "en": "⏳ Valid for {h}h {m}m"},
    "pay.valid_minutes": {"ru": "⏳ Действителен ещё {m} мин", "en": "⏳ Valid for {m} min"},
    "pay.expired_note": {"ru": "⌛ Срок платежа истёк", "en": "⌛ Payment window closed"},
    "pay.status_waiting": {"ru": "● Ожидаю перевод…", "en": "● Waiting for the transfer…"},
    "pay.status_confirming": {
        "ru": "🔎 Перевод найден, ждём подтверждений сети…",
        "en": "🔎 Transfer seen, waiting for confirmations…",
    },
    "pay.status_partial": {
        "ru": "⚠️ Пришло {paid} из {amount} USDT. Дошли остаток на тот же адрес.",
        "en": "⚠️ Received {paid} of {amount} USDT. Send the rest to the same address.",
    },
    "pay.status_done": {"ru": "✅ Оплата получена", "en": "✅ Payment received"},
    "pay.status_dead": {
        "ru": "❌ Платёж не состоялся. Можно создать новый.",
        "en": "❌ Payment did not go through. You can create a new one.",
    },
    "pay.success": {
        "ru": "✅ <b>Оплачено</b>\n\n{emoji} Тариф <b>{name}</b> активен до <b>{until}</b>.\nЛимит: <b>{n}</b> регистраций на каждый проект.",
        "en": "✅ <b>Paid</b>\n\n{emoji} Plan <b>{name}</b> is active until <b>{until}</b>.\nLimit: <b>{n}</b> registrations per project.",
    },
    "pay.creating": {"ru": "Создаю платёж…", "en": "Creating the payment…"},
    "pay.create_failed": {
        "ru": "Не удалось создать платёж. Попробуй ещё раз через минуту.",
        "en": "Could not create the payment. Try again in a minute.",
    },
    "pay.not_yet": {"ru": "Платёж ещё не получен", "en": "Payment not received yet"},
    "pay.cancelled": {"ru": "Платёж отменён", "en": "Payment cancelled"},
    "pay.not_yours": {
        "ru": "Счёт не найден или он не ваш.",
        "en": "Payment not found, or it is not yours.",
    },
    "pay.open_exists": {
        "ru": "У вас уже есть открытый счёт — оплатите его или отмените.",
        "en": "You already have an open payment — pay it or cancel it.",
    },
    "pay.disabled": {
        "ru": "Оплата пока не включена.",
        "en": "Payments are not enabled yet.",
    },
    "pay.sales_closed": {
        "ru": "Продажа тарифов закрыта.",
        "en": "Plan sales are closed.",
    },
    # Callback toasts (answer_callback_query): short, no HTML, no punctuation noise.
    "toast.banned": {"ru": "Доступ ограничен", "en": "Access restricted"},
    "toast.need_sub": {"ru": "Нужна подписка на канал", "en": "Channel subscription required"},
    "toast.sub_ok": {"ru": "Подписка подтверждена", "en": "Subscription confirmed"},
    "toast.not_subscribed": {
        "ru": "Ещё не подписан — подпишись и нажми снова",
        "en": "Not subscribed yet — subscribe and tap again",
    },
    "toast.projects": {"ru": "К списку проектов", "en": "Projects"},
    "toast.cancelled": {"ru": "Отменено", "en": "Cancelled"},
    "toast.project_unavailable": {"ru": "Проект недоступен", "en": "Project unavailable"},
    "toast.selected": {"ru": "Выбран: {name}", "en": "Selected: {name}"},
    "toast.keys_sent": {
        "ru": "Отправлено файлов: {n}",
        "en": "Files sent: {n}",
    },
    "toast.keys_none": {
        "ru": "Нечего выдавать — все ключи уже отправлены",
        "en": "Nothing to hand over — all keys were already sent",
    },
    "toast.keys_busy": {
        "ru": "Задача ещё идёт — ключи придут файлом, когда она закончится",
        "en": "The job is still running — the keys arrive as a file when it ends",
    },
    "toast.data": {"ru": "Аккаунты", "en": "Accounts"},
    "toast.proxy": {"ru": "Прокси", "en": "Proxies"},
    "toast.suggest_wait": {"ru": "Жду текст предложения", "en": "Send the suggestion text"},
    "toast.suggest_limit": {
        "ru": "Лимит: 1 предложение в сутки (UTC)",
        "en": "Limit: 1 suggestion per UTC day",
    },
    "btn.page_prev": {"ru": "◀", "en": "◀"},
    "btn.page_next": {"ru": "▶", "en": "▶"},
    "btn.page_at": {"ru": "{page}/{pages}", "en": "{page}/{pages}"},
    "btn.locked_show": {"ru": "🔒 Завершённые: {n}", "en": "🔒 Completed: {n}"},
    "btn.locked_hide": {"ru": "‹ Все проекты", "en": "‹ All projects"},
    # Tier gate, not the "🔒 Завершённые" one above: this project sets a
    # `min_ok_limit` this user's plan does not reach. The mini app shows such
    # rows greyed out instead of hiding them — a plan whose benefits are
    # invisible is a plan nobody buys.
    "projects.all_registered": {
        "ru": "Все {n} кошельков уже зарегистрированы здесь",
        "en": "All {n} wallets are already registered here",
    },
    "projects.tier_locked": {
        "ru": "Нужен тариф выше",
        "en": "Requires a higher plan",
    },
    "btn.proxy": {"ru": "🌐 Прокси", "en": "🌐 Proxies"},
    "btn.data": {"ru": "📦 Мои аккаунты", "en": "📦 My accounts"},
    "btn.queue": {"ru": "⏳ Очередь", "en": "⏳ Queue"},
    # Mini App button; only rendered when WEBAPP_URL is set.
    # It opened a progress screen when it was named; it now opens projects,
    # the queue, the mint calendar and the whole account. A label that
    # undersells what is behind it is a button people stop pressing.
    "btn.webapp": {"ru": "🚀 Открыть приложение", "en": "🚀 Open the app"},
    "btn.suggest": {"ru": "💡 Предложить проект", "en": "💡 Suggest a project"},
    "btn.back": {"ru": "« Назад", "en": "« Back"},
    "btn.to_projects": {"ru": "‹ К проектам", "en": "‹ To projects"},
    "btn.lang": {"ru": "🌐 Язык", "en": "🌐 Language"},
    "btn.upload_data": {
        "ru": "📤 Загрузить аккаунты",
        "en": "📤 Upload accounts",
    },
    "btn.run_reg": {
        "ru": "▶ Запустить регистрацию",
        "en": "▶ Start registration",
    },
    "btn.start_reg": {
        "ru": "▶ Запустить {n} {reg}",
        "en": "▶ Start {n} {reg}",
    },
    "btn.change_data": {
        "ru": "📦 Изменить аккаунты",
        "en": "📦 Edit accounts",
    },
    "btn.get_keys": {
        "ru": "🔑 Забрать ключи",
        "en": "🔑 Get my keys",
    },
    "btn.start_wallets": {
        "ru": "▶ Регистрация {n} кошельков",
        "en": "▶ Register {n} wallets",
    },
    "btn.confirm_start": {
        "ru": "✅ Да, создать {n}",
        "en": "✅ Yes, create {n}",
    },
    "btn.delete_data": {"ru": "🗑 Удалить данные", "en": "🗑 Delete data"},
    "btn.export_results": {"ru": "📤 Мои регистрации", "en": "📤 My registrations"},
    "toast.no_results": {
        "ru": "В «{name}» ещё нет успешных регистраций",
        "en": "No successful registrations in {name} yet",
    },
    "export.caption": {
        "ru": "📤 {name}: твои регистрации ({n})",
        "en": "📤 {name}: your registrations ({n})",
    },
    "export.sent": {"ru": "Файл отправлен", "en": "File sent"},
    "export.error": {
        "ru": "Не удалось отправить файл — попробуй ещё раз",
        "en": "Could not send the file — try again",
    },
    "data.confirm_clear": {
        "ru": "Точно удалить <b>все</b> аккаунты ({n})?\nФайл хранится только у тебя — восстановить будет нельзя, только загрузить заново.",
        "en": "Delete <b>all</b> accounts ({n})?\nThe file exists only on your side — this cannot be undone, only re-uploaded.",
    },
    "data.clear_yes": {"ru": "Да, удалить", "en": "Yes, delete"},
    "btn.twitter": {"ru": "🐦 Открыть X", "en": "🐦 Open X"},
    "btn.site": {"ru": "🌐 Открыть сайт", "en": "🌐 Open site"},
    "btn.mint": {"ru": "🗓 Страница минта", "en": "🗓 Mint page"},
    "btn.enqueue_reg": {
        "ru": "➕ В очередь: {name} ({n})",
        "en": "➕ Queue: {name} ({n})",
    },
    "btn.cancel_ask": {"ru": "⏹ Остановить", "en": "⏹ Stop"},
    "btn.cancel_yes": {
        "ru": "✅ Да, остановить",
        "en": "✅ Yes, stop",
    },
    "btn.cancel_no": {
        "ru": "‹ Не останавливать",
        "en": "‹ Don't stop",
    },
    "project.data_ready": {
        "ru": "📦 Данные: <b>{n}</b> акк. — нажми <b>▶ Начать рег</b> (Twitter/ссылки рег не стартуют).",
        "en": "📦 Data: <b>{n}</b> acc. — press <b>▶ Start reg</b> (Twitter/links do not start reg).",
    },
    "project.will_send": {
        "ru": "Отправится: до <b>{n}</b> {acc}",
        "en": "Up to <b>{n}</b> {acc} will be sent",
    },
    "project.slots": {
        "ru": "Доступно слотов: <b>{n}</b>",
        "en": "Slots available: <b>{n}</b>",
    },
    "project.proxy_shared_label": {
        "ru": "Прокси: общие",
        "en": "Proxies: shared",
    },
    "project.proxy_own_label": {
        "ru": "Прокси: свои ({n})",
        "en": "Proxies: own ({n})",
    },
    "project.requirements": {
        "ru": "Нужно для регистрации:",
        "en": "What you need:",
    },
    "project.mint": {
        "ru": "🗓 Минт: {info}",
        "en": "🗓 Mint: {info}",
    },
    "project.selfsourced_send": {
        "ru": "Бот создаст кошельки: <b>{n}</b> (загружать ничего не нужно)",
        "en": "The bot will create <b>{n}</b> wallets (nothing to upload)",
    },
    "project.selfsourced_need": {
        "ru": "Нужно <b>{need}</b> свободных слотов, осталось {left}.",
        "en": "Needs <b>{need}</b> free slots, {left} left.",
    },
    "project.confirm_body": {
        "ru": (
            "Создать <b>{n}</b> кошельков и подать заявки?\n"
            "Это спишет <b>{n}</b> слотов из {left}."
        ),
        "en": (
            "Create <b>{n}</b> wallets and file the entries?\n"
            "This spends <b>{n}</b> of your {left} slots."
        ),
    },
    "project.confirm_keys": {
        "ru": (
            "🔑 Приватные ключи всех кошельков придут файлом по окончании — "
            "сохрани его, второй раз бот их выдать не сможет."
        ),
        "en": (
            "🔑 Private keys for every wallet arrive as a file when the run ends — "
            "save it, the bot cannot hand them over twice."
        ),
    },
    "project.no_data_hint": {
        "ru": "Сначала загрузите аккаунты — потом запустите регистрацию.",
        "en": "Upload your accounts first — then start registration.",
    },
    "project.will_send_capped": {
        "ru": "Отправится: до <b>{n}</b> {acc} из {total} (лимит слотов)",
        "en": "Up to <b>{n}</b> of {total} {acc} will be sent (slot limit)",
    },
    "project.limit_done": {
        "ru": "Лимит регистраций выполнен.",
        "en": "Registration limit reached.",
    },
    "project.health_config": {
        "ru": "Статус: 🟡 требуется настройка администратором.",
        "en": "Status: 🟡 administrator setup required.",
    },
    "project.health_closed": {
        "ru": "Статус: 🔴 регистрация недоступна.",
        "en": "Status: 🔴 registration unavailable.",
    },
    "project.proxy_shared_n": {
        "ru": "Прокси: общие ({n})",
        "en": "Proxies: shared ({n})",
    },
    "project.proxy_own_n": {
        "ru": "Прокси: свои ({n})",
        "en": "Proxies: own ({n})",
    },
    "project.proxy_none": {
        "ru": "Прокси: ❌ нет",
        "en": "Proxies: ❌ none",
    },
    "project.proxy_corrupt": {
        "ru": "Прокси: ❌ файл повреждён",
        "en": "Proxies: ❌ file corrupt",
    },
    "project.proxy_none_hint": {
        "ru": "Без прокси регистрация невозможна. Загрузите свои (/proxy) либо дождитесь общих.",
        "en": "Registration is impossible without proxies. Upload your own (/proxy) or wait for shared.",
    },
    "project.proxy_none_hint_own_only": {
        "ru": "Без своих прокси регистрация сейчас невозможна — общий пул выключен. Загрузите /proxy.",
        "en": "Registration needs your own proxies right now — the shared pool is off. Upload /proxy.",
    },
    "project.fmt_help": {
        "ru": "Формат строки",
        "en": "Line format",
    },
    "req.handle": {"ru": "Имя пользователя X", "en": "X (Twitter) username"},
    "req.wallet": {"ru": "Адрес кошелька (EVM)", "en": "Wallet address (EVM)"},
    "req.link": {"ru": "Ссылка на пост/retweet", "en": "Link to a post/retweet"},
    "queue.bar": {
        "ru": "<code>{bar}</code> {done} из {total}",
        "en": "<code>{bar}</code> {done} of {total}",
    },
    "queue.stat_ok": {"ru": "✅ Успешно: <b>{n}</b>", "en": "✅ Successful: <b>{n}</b>"},
    "queue.stat_already": {
        "ru": "ℹ️ Уже зарегистрированы: <b>{n}</b>",
        "en": "ℹ️ Already registered: <b>{n}</b>",
    },
    "queue.stat_bad": {
        "ru": "⚠️ Отклонены проектом: <b>{n}</b>",
        "en": "⚠️ Rejected by project: <b>{n}</b>",
    },
    "queue.stat_fail": {
        "ru": "❌ Технические ошибки: <b>{n}</b>",
        "en": "❌ Technical errors: <b>{n}</b>",
    },
    "queue.left_rows": {"ru": "Осталось: <b>{n}</b>", "en": "Remaining: <b>{n}</b>"},
    "queue.accounts": {"ru": "Аккаунтов: <b>{n}</b>", "en": "Accounts: <b>{n}</b>"},
    "queue.job_id": {"ru": "Задание #{id}", "en": "Job #{id}"},
    "queue.next_project": {"ru": "Далее: <b>{name}</b>", "en": "Next: <b>{name}</b>"},
    "queue.wait_after": {
        "ru": "Начнётся после: <b>{name}</b>",
        "en": "Starts after: <b>{name}</b>",
    },
    "queue.wait_worker": {
        "ru": "Ждём свободный обработчик…",
        "en": "Waiting for a free worker…",
    },
    "queue.chain_pos": {
        "ru": "В вашей очереди: {pos} из {total}",
        "en": "In your queue: {pos} of {total}",
    },
    "queue.running_title": {"ru": "🔄 Идёт регистрация", "en": "🔄 Registration in progress"},
    "queue.queued_title": {"ru": "⏳ В очереди", "en": "⏳ Queued"},
    "queue.active_title": {"ru": "🔄 Активные регистрации", "en": "🔄 Active registrations"},
    "queue.done_title": {"ru": "✅ Регистрация завершена", "en": "✅ Registration finished"},
    "queue.cancelled_title": {"ru": "⏹ Остановлено", "en": "⏹ Stopped"},
    "queue.done_before_stop": {
        "ru": "Успело отправиться: {done} из {total}",
        "en": "Sent before stop: {done} of {total}",
    },
    "queue.skipped_dup_note": {
        "ru": "ℹ️ Пропущено (уже в боте): {n}",
        "en": "ℹ️ Skipped (already in bot): {n}",
    },
    "queue.limit_stop_note": {
        "ru": "⚠️ Остановлено: достигнут лимит успешных регистраций.",
        "en": "⚠️ Stopped: successful registration limit reached.",
    },
    "queue.cancel_confirm": {
        "ru": "Остановить регистрацию после текущего аккаунта?",
        "en": "Stop registration after the current account?",
    },
    "queue.nothing_to_queue": {
        "ru": "Нечего ставить в очередь.",
        "en": "Nothing to queue.",
    },
    "queue.no_proxies": {
        "ru": "Нет доступных прокси — регистрация в «{name}» невозможна. "
        "Загрузите свои (/proxy) либо дождитесь общих.",
        "en": "No proxies available — registration in «{name}» is impossible. "
        "Upload your own (/proxy) or wait for shared.",
    },
    "queue.no_proxies_own_only": {
        "ru": "Общий пул прокси выключен — регистрация в «{name}» только через свои. "
        "Загрузите /proxy.",
        "en": "The shared proxy pool is off — registration in «{name}» needs your own proxies. "
        "Upload /proxy.",
    },
    # Own /proxy file is in use and keeps answering ProxyError. Once per job
    # after OWN_PROXY_FAIL_ALERT_STREAK consecutive proxy FAILs.
    "queue.own_proxy_dead": {
        "ru": "⚠️ Ваши прокси не работают (ошибки подключения). "
        "Загрузите другие через /proxy.",
        "en": "⚠️ Your proxies are not working (connection errors). "
        "Upload new ones via /proxy.",
    },
    "queue.project_disabled": {
        "ru": "Проект «{name}» выключен администратором.",
        "en": "Project «{name}» was disabled by the admin.",
    },
    "queue.skipped_lines_note": {
        "ru": "⚠️ Пропущено строк: <b>{n}</b> (см. /data)",
        "en": "⚠️ Skipped lines: <b>{n}</b> (see /data)",
    },
    "queue.starting": {
        "ru": "Старт: {name}",
        "en": "Starting: {name}",
    },
    # VIP «all projects» batch
    "btn.queue_all": {
        "ru": "⚡ Во все проекты ({n})",
        "en": "⚡ All projects ({n})",
    },
    "btn.cancel_all_ask": {
        "ru": "⏹ Остановить всё ({n})",
        "en": "⏹ Stop everything ({n})",
    },
    "queue.batch_progress": {
        "ru": "📦 Батч: готово проектов {done} из {total}",
        "en": "📦 Batch: {done} of {total} projects done",
    },
    "queue.more_jobs": {
        "ru": "… и ещё {n} в очереди",
        "en": "… and {n} more queued",
    },
    "queue.batch_starting": {
        "ru": "Ставлю в очередь все доступные проекты…",
        "en": "Queueing every available project…",
    },
    "queue.batch_started": {
        "ru": "🚀 В очереди проектов: <b>{n}</b> · аккаунтов суммарно: <b>{rows}</b>",
        "en": "🚀 Projects queued: <b>{n}</b> · accounts in total: <b>{rows}</b>",
    },
    "queue.batch_skipped": {
        "ru": "Пропущено: {n} (нет слотов, закрыт или уже в очереди)",
        "en": "Skipped: {n} (no slots, closed, or already queued)",
    },
    "queue.batch_busy": {
        "ru": "Батч уже идёт — дождись окончания или останови его.",
        "en": "A batch is already running — wait for it or stop it.",
    },
    "queue.batch_none": {
        "ru": "Нечего запускать: нет открытых проектов со свободными слотами.",
        "en": "Nothing to start: no open projects with free slots.",
    },
    "queue.batch_vip_only": {
        "ru": "Запуск во все проекты доступен только VIP.",
        "en": "Starting all projects at once is VIP-only.",
    },
    "queue.batch_cancel_confirm": {
        "ru": "Остановить <b>все</b> задания ({n})? Текущие аккаунты долетят до конца.",
        "en": "Stop <b>all</b> jobs ({n})? Accounts in flight will finish.",
    },
    "queue.batch_cancelled": {
        "ru": "Остановлено заданий: {n}",
        "en": "Jobs stopped: {n}",
    },
    "queue.ready_start": {
        "ru": "Нет активных заданий.\n"
        "Аккаунты: <b>{n}</b> · проект: <b>{name}</b>\n"
        "Нажми <b>▶ Запустить регистрацию</b> — или выбери другой проект в /start и добавь в очередь.",
        "en": "No active jobs.\n"
        "Accounts: <b>{n}</b> · project: <b>{name}</b>\n"
        "Press <b>▶ Start registration</b> — or pick another project in /start to queue.",
    },
    "queue.can_enqueue": {
        "ru": "➕ Можно добавить в очередь: <b>{name}</b> ({n} акк.) — кнопка ниже.",
        "en": "➕ Can queue: <b>{name}</b> ({n} acc.) — button below.",
    },
    "data.title": {"ru": "<b>📦 Данные аккаунтов</b>", "en": "<b>📦 Account data</b>"},
    "data.empty": {
        "ru": "Файла нет. Пришли <b>.txt</b>:\n<code>handle|0xWallet</code> или <code>handle,0xWallet</code>\n1 строка = 1 аккаунт.",
        "en": "No file yet. Send a <b>.txt</b>:\n<code>handle|0xWallet</code> or <code>handle,0xWallet</code>\n1 line = 1 account.",
    },
    "data.has": {
        "ru": "Сейчас: <b>{n}</b> аккаунтов.\nПришли новый .txt чтобы заменить.\n"
        "Выбери проект → откроется экран → <b>▶ Запустить регистрацию</b> (не авто).",
        "en": "Now: <b>{n}</b> accounts.\nSend a new .txt to replace.\n"
        "Pick a project → project screen → <b>▶ Start registration</b> (not auto).",
    },
    "data.need": {
        "ru": "Сначала загрузи данные: /data\nФормат: <code>handle|0xWallet</code> или <code>handle,0xWallet</code>",
        "en": "Upload data first: /data\nFormat: <code>handle|0xWallet</code> or <code>handle,0xWallet</code>",
    },
    "btn.data_edit": {"ru": "✏️ Изменить строки", "en": "✏️ Edit rows"},
    "data.edit_title": {
        "ru": "<b>📦 Аккаунты — строка · хендл · кошелёк</b> (стр. {page} из {pages})",
        "en": "<b>📦 Accounts — row · handle · wallet</b> (page {page} of {pages})",
    },
    "data.del_row_btn": {"ru": "🗑 {n}", "en": "🗑 {n}"},
    "data.del_confirm": {
        "ru": "Удалить строку {n}?\n\n<code>{handle}</code>\n<code>{wallet}</code>",
        "en": "Delete row {n}?\n\n<code>{handle}</code>\n<code>{wallet}</code>",
    },
    "btn.del_row_yes": {"ru": "✅ Да, удалить", "en": "✅ Yes, delete"},
    "btn.del_row_no": {"ru": "‹ Не удалять", "en": "‹ Keep it"},
    "toast.data_row_deleted": {
        "ru": "Строка {n} удалена. Осталось: {n_left}",
        "en": "Row {n} deleted. Left: {n_left}",
    },
    "toast.data_bad_row": {
        "ru": "Такой строки нет — список обновился?",
        "en": "No such row — the list changed?",
    },
    "keys.title": {"ru": "<b>🔑 Свой ключ капчи</b>", "en": "<b>🔑 Own captcha key</b>"},
    "keys.help": {
        "ru": "Проекты с капчей требуют платного решения капчи.\nСо своим ключом регистрация в них идёт <b>за твой счёт</b> — лимиты и цена обычные, мой баланс не тратится.\nПоддержка: Capsolver, 2Captcha, Anti-Captcha.",
        "en": "Captcha projects need a paid solve per row.\nWith your own key they run <b>on your balance</b> — same limits, my balance untouched.\nSupported: Capsolver, 2Captcha, Anti-Captcha.",
    },
    "keys.help_multi": {
        "ru": "Проекты с капчей требуют платного решения капчи.\nСо своими ключами регистрация в них идёт <b>за твой счёт</b> — лимиты и цена обычные.\nМожно держать до <b>3 ключей</b> — по одному на провайдера (Capsolver, 2Captcha, Anti-Captcha). Решает первый живой; опустевший ключ пропускается, строка идёт на следующий.",
        "en": "Captcha projects need a paid solve per row.\nWith your own keys they run <b>on your balance</b> — same limits.\nYou may keep up to <b>3 keys</b> — one per provider (Capsolver, 2Captcha, Anti-Captcha). The first live key solves; an empty one is skipped and the row moves to the next.",
    },
    "keys.multi_prompt": {
        "ru": "Пришли ключи — <b>по одному в строке</b>, до 3 штук:\n<code>capsolver CAP-...\n2captcha abc123...</code>\nМожно писать провайдера через пробел, двоеточие или запятую. Проверю баланс каждого.",
        "en": "Send your keys — <b>one per line</b>, up to 3:\n<code>capsolver CAP-...\n2captcha abc123...</code>\nThe provider may be separated by a space, colon or comma. Each key's balance is checked.",
    },
    "keys.multi_saved": {
        "ru": "✅ Сохранено ключей: {n}. Проекты с капчей открыты.",
        "en": "✅ Keys saved: {n}. Captcha projects are open now.",
    },
    "keys.multi_bad_line": {
        "ru": "❌ Строка {n} не принята: {reason}",
        "en": "❌ Line {n} rejected: {reason}",
    },
    "keys.multi_line_hint": {
        "ru": "Формат строки: <code>провайдер ключ</code> (например, <code>2captcha abc123...</code>).",
        "en": "Line format: <code>provider key</code> (e.g. <code>2captcha abc123...</code>).",
    },
    "keys.key_deleted": {
        "ru": "🗑 Ключ {provider} удалён.",
        "en": "🗑 {provider} key deleted.",
    },
    "keys.has": {
        "ru": "Ключ: <code>{masked}</code> (баланс: {balance})",
        "en": "Key: <code>{masked}</code> (balance: {balance})",
    },
    "keys.none": {"ru": "Ключ не задан.", "en": "No key set."},
    "keys.send_prompt": {
        "ru": "Пришли ключ <b>{provider}</b> одним сообщением.\nПроверю баланс — если ключ живой, сохраню (в логах и чате он не показывается).",
        "en": "Send your <b>{provider}</b> key as one message.\nI will check the balance — a live key is stored (never shown in logs or chat).",
    },
    "keys.saved": {
        "ru": "✅ Ключ {provider} сохранён, баланс: {balance}. Проекты с капчей открыты.",
        "en": "✅ {provider} key saved, balance: {balance}. Captcha projects are open now.",
    },
    "keys.invalid": {
        "ru": "❌ Ключ не принят: {reason}\nПопробуй другой ключ или другого провайдера — /keys",
        "en": "❌ Key rejected: {reason}\nTry another key or provider — /keys",
    },
    "keys.cleared": {
        "ru": "🗑 Ключ удалён. Проекты с капчей снова закрыты.",
        "en": "🗑 Key deleted. Captcha projects are closed again.",
    },
    "keys.confirm_clear": {
        "ru": "Удалить ключ капчи? Проекты с капчей станут недоступны.",
        "en": "Delete the captcha key? Captcha projects will close.",
    },
    "keys.network_down": {
        "ru": "Провайдер не ответил, попробуй ещё раз позже.",
        "en": "The provider did not answer, try again later.",
    },
    "btn.keys": {"ru": "🔑 Ключ капчи", "en": "🔑 Captcha key"},
    "btn.keys_multi": {"ru": "📥 Вставить ключи", "en": "📥 Paste keys"},
    "btn.delete_key": {"ru": "🗑 Удалить все ключи", "en": "🗑 Delete all keys"},
    "autoreg.title": {"ru": "<b>⚡ Авторег</b>", "en": "<b>⚡ Auto-register</b>"},
    "autoreg.help": {
        "ru": "Новый проект появляется в боте → регистрация твоих аккаунтов (\U0001F4E4 /data) запускается сама, в рамках твоего лимита.\nПо завершении придёт личное уведомление с результатом.\nПрокси — твои или общие по твоему тиру; проекты с капчей — только со своим ключом (/keys).",
        "en": "A new project appears in the bot → your accounts (\U0001F4E4 /data) are registered automatically, within your limit.\nYou get a personal DM with the result when it ends.\nProxies — yours or shared per your tier; captcha projects — only with your own key (/keys).",
    },
    "autoreg.state_on": {
        "ru": "Статус: <b>включён</b> ✅",
        "en": "Status: <b>on</b> ✅",
    },
    "autoreg.state_off": {
        "ru": "Статус: <b>выключен</b>",
        "en": "Status: <b>off</b>",
    },
    "autoreg.data_need": {
        "ru": "⚠️ Для авторега нужны аккаунты: /data",
        "en": "⚠️ Auto-register needs accounts: /data",
    },
    "autoreg.need_data_turn_on": {
        "ru": "Авторег включён, но аккаунтов нет — загрузи /data, иначе запускать нечего.",
        "en": "Auto-register is on but there are no accounts — upload /data, otherwise there is nothing to run.",
    },
    "autoreg.toggled_on": {"ru": "⚡ Авторег включён.", "en": "⚡ Auto-register is on."},
    "autoreg.toggled_off": {"ru": "Авторег выключен.", "en": "Auto-register is off."},
    "autoreg.done": {
        "ru": "🏁 <b>Регистрация завершена</b>\nПроект: <b>{project}</b>\nЗарегистрировалось: <b>{registered}</b> из {total}",
        "en": "🏁 <b>Registration finished</b>\nProject: <b>{project}</b>\nRegistered: <b>{registered}</b> of {total}",
    },
    "btn.autoreg": {"ru": "⚡ Авторег", "en": "⚡ Auto-register"},
    "btn.autoreg_on": {"ru": "✅ Включить авторег", "en": "✅ Turn on"},
    "btn.autoreg_off": {"ru": "⏸ Выключить авторег", "en": "⏸ Turn off"},
    "data.saved": {
        "ru": "Сохранено: {n} аккаунтов (handle|wallet)",
        "en": "Saved: {n} accounts (handle|wallet)",
    },
    "data.upload_done": {
        "ru": "✅ {message}\n\nТеперь выбери проект в /start и запусти очередь. Tweet URL будут сгенерированы при необходимости.",
        "en": "✅ {message}\n\nNow pick a project in /start and start the queue. Tweet URLs will be generated when needed.",
    },
    "data.error.format": {
        "ru": "нужен формат handle|0xWallet или handle,0xWallet",
        "en": "expected handle|0xWallet or handle,0xWallet",
    },
    "data.error.required": {
        "ru": "handle и wallet обязательны",
        "en": "handle and wallet are required",
    },
    "data.error.handle": {
        "ru": "некорректный X handle (1–15, A-Za-z0-9_)",
        "en": "invalid X handle (1–15, A-Za-z0-9_)",
    },
    "data.error.wallet": {
        "ru": "нет валидного wallet (0x + 40 hex)",
        "en": "invalid wallet (0x + 40 hex required)",
    },
    "data.error.too_many": {
        "ru": "больше {max} строк — разбей файл",
        "en": "more than {max} lines — split the file",
    },
    "data.error.duplicate": {
        "ru": "дубликат wallet {wallet}…",
        "en": "duplicate wallet {wallet}…",
    },
    "data.error.no_rows": {"ru": "нет строк с данными", "en": "no data rows"},
    "data.error.empty_file": {"ru": "файл пустой", "en": "the file is empty"},
    "data.error.too_big": {
        "ru": "файл слишком большой (лимит {kb} KB)",
        "en": "file is too large (limit {kb} KB)",
    },
    "data.error.encoding": {
        "ru": "не удалось прочитать кодировку (UTF-8)",
        "en": "could not decode the file (UTF-8)",
    },
    "data.error.rejected": {
        "ru": "Файл не принят. Старый список без изменений.",
        "en": "File rejected. The previous list was not changed.",
    },
    "data.error.more": {"ru": "… и ещё {n}", "en": "… and {n} more"},
    "data.error.busy": {
        "ru": "Уже обрабатываю запрос, подожди…",
        "en": "A request is already being processed; please wait…",
    },
    "data.error.download": {
        "ru": "Не удалось скачать файл: <code>{error}</code>",
        "en": "Could not download the file: <code>{error}</code>",
    },
    "data.deleted": {"ru": "Удалено", "en": "Deleted"},
    "rate.cooldown": {
        "ru": "Не так быстро. Одно сообщение в минуту — следующее приму через {seconds} с.",
        "en": "Not so fast. One message per minute — the next one lands in {seconds}s.",
    },
    "rate.too_fast": {
        "ru": "Слишком часто. Подожди немного и повтори.",
        "en": "Too many requests. Wait a little and try again.",
    },
    "bulk.error.no_schema": {
        "ru": "у проекта не задана схема bulk_columns",
        "en": "the project has no bulk_columns schema",
    },
    "bulk.error.empty_line": {"ru": "пустая строка", "en": "empty line"},
    "bulk.error.too_few": {
        "ru": "мало полей: нужно минимум <b>{min}</b>, пришло <b>{got}</b>. Не хватает: {missing}. Ожидаемый порядок: <code>{order}</code>",
        "en": "too few fields: at least <b>{min}</b> required, <b>{got}</b> received. Missing: {missing}. Expected order: <code>{order}</code>",
    },
    "bulk.error.extra": {
        "ru": "лишние поля: пришло <b>{got}</b>, в схеме <b>{max}</b>. Ожидаемый порядок: <code>{order}</code>. Убери лишние колонки или используй формат из /help.",
        "en": "extra fields: <b>{got}</b> received, schema has <b>{max}</b>. Expected order: <code>{order}</code>. Remove extra columns or use the /help format.",
    },
    "bulk.error.missing": {
        "ru": "нет поля «{label}» (колонка {col})",
        "en": "missing “{label}” (column {col})",
    },
    "bulk.error.empty": {
        "ru": "пустое поле «{label}» (колонка {col}). Заполни или удали строку.",
        "en": "empty “{label}” (column {col}). Fill it in or remove the row.",
    },
    "bulk.error.file_empty": {"ru": "файл пустой", "en": "the file is empty"},
    "bulk.error.too_big": {
        "ru": "файл слишком большой (лимит {kb} KB). Разбей на несколько файлов.",
        "en": "file is too large (limit {kb} KB). Split it into multiple files.",
    },
    "bulk.error.encoding": {
        "ru": "не удалось прочитать кодировку (нужен UTF-8 или CP1251)",
        "en": "could not decode the file (UTF-8 or CP1251 required)",
    },
    "bulk.error.line_limit": {
        "ru": "превышен лимит строк в файле (<b>{max}</b>). Оставь первые {max} записей или разбей файл.",
        "en": "file line limit exceeded (<b>{max}</b>). Keep the first {max} entries or split the file.",
    },
    "bulk.error.lines_skipped": {
        "ru": "ещё ~{n} строк пропущено из-за лимита файла",
        "en": "about {n} more lines skipped because of the file limit",
    },
    "bulk.error.validation": {
        "ru": "проверка данных: {error}",
        "en": "validation: {error}",
    },
    "bulk.error.no_data": {
        "ru": "в файле нет данных. Нужны строки вида <code>handle|0xWallet|…</code> (по одной записи на строку). Пустые и строки с <code>#</code> игнорируются.",
        "en": "the file has no data. Use <code>handle|0xWallet|…</code>, one entry per line. Blank lines and <code>#</code> comments are ignored.",
    },
    "bulk.help": {
        "ru": "📎 .txt — тот же формат, 1 строка = 1 запись",
        "en": "📎 .txt — same format, one entry per line",
    },
    # Two different screens wanted a title here and both took the same key, so
    # the later definition silently won and `bot/bulk.py` — which passes no
    # page numbers — rendered the literal text «стр. {page}/{pages}» at users.
    # The paginated one keeps the shared name; this one is the file report.
    "bulk.errors_file_title": {
        "ru": "<b>Ошибки в файле</b> (исправь и пришли снова):",
        "en": "<b>File errors</b> (fix and send again):",
    },
    "bulk.error_line": {
        "ru": "• Строка <b>{line}</b>: <code>{preview}</code>",
        "en": "• Line <b>{line}</b>: <code>{preview}</code>",
    },
    "bulk.more_errors": {
        "ru": "\n… и ещё {n} ошибок.",
        "en": "\n… and {n} more errors.",
    },
    "bulk.select_project": {
        "ru": "Сначала выбери проект (/start) для data-файла или открой /proxy, чтобы загрузить прокси.",
        "en": "Pick a project in /start for the data file, or open /proxy to upload proxies.",
    },
    "bulk.not_configured": {
        "ru": "Для <b>{project}</b> bulk не настроен. Отправь одну строку текстом.",
        "en": "Bulk upload is not configured for <b>{project}</b>. Send one line as text.",
    },
    "bulk.header": {
        "ru": "📎 Файл для <b>{project}</b>\nВалидных: <b>{valid}</b> · ошибок: <b>{bad}</b>\nСлотов: <b>{slots}</b> / {limit}\n",
        "en": "📎 File for <b>{project}</b>\nValid: <b>{valid}</b> · errors: <b>{bad}</b>\nSlots: <b>{slots}</b> / {limit}\n",
    },
    "bulk.format_ok": {"ru": "Формат OK, отправляю…", "en": "Format OK, submitting…"},
    "bulk.nothing": {"ru": "Нечего отправлять — исправь ошибки.", "en": "Nothing to submit — fix the errors."},
    "bulk.processing": {
        "ru": "⏳ Отправляю <b>{n}</b> строк…",
        "en": "⏳ Submitting <b>{n}</b> rows…",
    },
    "queue.eta": {"ru": "⏳ Осталось ~{eta}", "en": "⏳ ~{eta} left"},
    "queue.eta_starts": {"ru": "⏳ Старт примерно через ~{eta}", "en": "⏳ Starts in ~{eta}"},
    "queue.eta_hm": {"ru": "{h} ч {m} мин", "en": "{h}h {m}m"},
    "queue.eta_m": {"ru": "{m} мин", "en": "{m} min"},
    "bulk.progress": {
        "ru": "⏳ <b>{project}</b>\n<code>{bar}</code> {done} из {total}\n✅ {ok}",
        "en": "⏳ <b>{project}</b>\n<code>{bar}</code> {done} of {total}\n✅ {ok}",
    },
    "bulk.errors_title": {
        "ru": "<b>📋 Ошибки</b> · стр. {page}/{pages}",
        "en": "<b>📋 Errors</b> · page {page}/{pages}",
    },
    "btn.show_errors": {"ru": "📋 Показать ошибки ({n})", "en": "📋 Show errors ({n})"},
    "btn.back_to_summary": {"ru": "‹ К итогу", "en": "‹ Back to summary"},
    "bulk.fail_line": {
        "ru": "• стр.{line} <code>{handle}</code> → {status}: {detail}",
        "en": "• line {line} <code>{handle}</code> → {status}: {detail}",
    },
    "bulk.summary": {
        "ru": "✅ <b>Готово</b> · {project}\nOK: <b>{ok}</b> · ALREADY: <b>{already}</b> · BAD_RESP: <b>{bad_resp}</b> · FAIL: <b>{fail}</b> · BUSY: <b>{busy}</b>\nФормат-ошибок: {format_bad}\nУспешных: <b>{total_ok}</b> / {limit} (осталось {left})",
        "en": "✅ <b>Done</b> · {project}\nOK: <b>{ok}</b> · ALREADY: <b>{already}</b> · BAD_RESP: <b>{bad_resp}</b> · FAIL: <b>{fail}</b> · BUSY: <b>{busy}</b>\nFormat errors: {format_bad}\nSuccessful: <b>{total_ok}</b> / {limit} ({left} left)",
    },
    "bulk.dedup_note": {
        "ru": "\nℹ️ Уже в боте (API пропущен): <b>{n}</b>",
        "en": "\nℹ️ Already in the bot (API skipped): <b>{n}</b>",
    },
    "bulk.skipped_note": {
        "ru": "\n⚠️ Не отправлено (лимит/cap): <b>{n}</b>",
        "en": "\n⚠️ Not submitted (limit/cap): <b>{n}</b>",
    },
    "file.txt_required": {
        "ru": "Нужен файл <b>.txt</b>.\nСейчас: <code>{name}</code>",
        "en": "A <b>.txt</b> file is required.\nReceived: <code>{name}</code>",
    },
    "queue.busy": {
        "ru": "Сейчас идёт задание #{id}.\n"
        "• Можно <b>добавить другой проект</b> в свою очередь (до 3)\n"
        "• /queue — что сейчас и что ждёт\n"
        "• /cancel — остановить текущий",
        "en": "A job is running now: #{id}.\n"
        "• You can <b>queue another project</b> (up to 3)\n"
        "• /queue — running + waiting\n"
        "• /cancel — stop current",
    },
    "queue.full": {
        "ru": "Твоя очередь полная ({cur}/{max}).\n"
        "Дождись конца или /cancel — потом снова выбери проект.",
        "en": "Your queue is full ({cur}/{max}).\n"
        "Wait for a finish or /cancel — then pick a project again.",
    },
    "queue.same_project": {
        "ru": "Проект <b>{project}</b> уже в работе/очереди (задание #{id}, {status}).\n"
        "Не дублируем. Выбери <b>другой</b> проект или /queue.",
        "en": "Project <b>{project}</b> already queued/running (job #{id}, {status}).\n"
        "No duplicate. Pick <b>another</b> project or /queue.",
    },
    "queue.chained": {
        "ru": "✅ <b>Поставлено в очередь</b> · задание #{id} · {project}\n"
        "Сейчас идёт #{prev_id} ({prev}).\n"
        "Когда оно закончится — это стартует сам.\n"
        "В очереди: {n}/{max}. /queue — список.",
        "en": "✅ <b>Queued</b> · job #{id} · {project}\n"
        "Now running #{prev_id} ({prev}).\n"
        "When it finishes — this one starts automatically.\n"
        "Chain: {n}/{max}. /queue — list.",
    },
    "queue.none": {
        "ru": "Нет активных заданий.\n"
        "1) /data — файл handle|wallet\n"
        "2) выбери проект в /start → регистрация из файла\n"
        "Можно выбрать до 3 проектов подряд — они пойдут по очереди.",
        "en": "No active jobs.\n"
        "1) /data — handle|wallet file\n"
        "2) pick a project in /start → registration from file\n"
        "Up to 3 projects in a row — they run one after another.",
    },
    "queue.idle_need_data": {
        "ru": "Нет активных заданий.\nПроект выбран: <b>{project}</b>\n"
        "Нужны аккаунты: /data (<code>handle|0xWallet</code>)",
        "en": "No active jobs.\nProject selected: <b>{project}</b>\n"
        "Need accounts: /data (<code>handle|0xWallet</code>)",
    },
    "queue.idle_need_project": {
        "ru": "Нет активных заданий.\nАккаунты: <b>{n}</b>.\n"
        "Выбери проект в /start → <b>▶ Запустить регистрацию</b> (до 3 в очереди).",
        "en": "No active jobs.\nAccounts: <b>{n}</b>.\n"
        "Pick a project in /start → <b>▶ Start registration</b> (up to 3 queued).",
    },
    "queue.restarting": {
        "ru": "▶ Старт из /data · проект <b>{project}</b> · {n} акк.",
        "en": "▶ Starting from /data · project <b>{project}</b> · {n} acc.",
    },
    "queue.restart_again": {
        "ru": "▶ Снова из /data",
        "en": "▶ Start again from /data",
    },
    "queue.restart_error": {
        "ru": "⏹ Задание #{id} · <code>{project}</code>\n"
        "Бот был перезапущен, поэтому задание безопасно остановлено и не будет "
        "отправлено повторно автоматически.\n"
        "Файл /data сохранён: выберите проект снова или используйте /queue.",
        "en": "⏹ Job #{id} · <code>{project}</code>\n"
        "The bot restarted, so the job was stopped safely and will not be "
        "replayed automatically.\n"
        "Your /data file is kept: pick the project again or use /queue.",
    },
    "queue.cancelled": {
        "ru": "⏹ Задание #{id} остановлено.\n"
        "Аккаунты сохранены. Следующее в вашей очереди (если было) пойдёт сам.\n"
        "/queue — статус · или выберите проект снова.",
        "en": "⏹ Job #{id} stopped.\n"
        "Accounts kept. Next in your chain (if any) starts on its own.\n"
        "/queue — status · or pick a project again.",
    },
    "queue.cancelling": {
        "ru": "⏹ Останавливаю задание #{id} после текущего аккаунта…\n"
        "Остальные в вашей очереди не трогаем.",
        "en": "⏹ Stopping job #{id} after the current account…\n"
        "Other jobs in your chain stay queued.",
    },
    "queue.cancel_fail": {
        "ru": "Не удалось отменить: {reason}",
        "en": "Could not cancel: {reason}",
    },
    "btn.cancel_job": {"ru": "⏹ Отменить", "en": "⏹ Cancel"},
    "dedup.skip": {
        "ru": "ℹ️ <b>Уже было</b> · {name}\n"
        "wallet: <code>{wallet}</code>\n"
        "В боте уже есть <b>{status}</b> по этому кошельку "
        "(любой аккаунт, {when}).\n"
        "Слот <b>не</b> списан. API не дергали.",
        "en": "ℹ️ <b>Already on file</b> · {name}\n"
        "wallet: <code>{wallet}</code>\n"
        "Bot already has <b>{status}</b> for this wallet "
        "(any account, {when}).\n"
        "Slot <b>not</b> used. No API call.",
    },
    "dedup.queue_note": {
        "ru": "ℹ️ Пропущено уже зарегистрированных в боте: <b>{n}</b> "
        "(успешных/уже было по проекту, глобально).",
        "en": "ℹ️ Skipped already registered in bot: <b>{n}</b> "
        "(successful/already for project, global).",
    },
    "dedup.queue_all": {
        "ru": "ℹ️ Все <b>{n}</b> кошельков уже были OK/ALREADY в боте "
        "(глобально) — нечего слать.",
        "en": "ℹ️ All <b>{n}</b> wallets already OK/ALREADY in bot "
        "(global) — nothing to send.",
    },
    # Gates
    "banned": {
        "ru": "Доступ ограничен.",
        "en": "Access restricted.",
    },
    "no_access": {"ru": "Нет доступа.", "en": "Access denied."},
    # Channel subscription gate (shown before any feature on /start)
    "gate.text": {
        "ru": "🔒 Регистрация доступна только подписчикам канала.\n\n1. Подпишись: {link}\n2. Нажми «Проверить подписку»\n\nБез подписки отправить заявку нельзя.",
        "en": "🔒 Registration is only available to channel subscribers.\n\n1. Subscribe: {link}\n2. Tap «Check subscription»\n\nYou can't submit without subscribing.",
    },
    "gate.subscribe_btn": {"ru": "📢 Подписаться на канал", "en": "📢 Subscribe to channel"},
    "gate.check_btn": {"ru": "✅ Проверить подписку", "en": "✅ Check subscription"},
    "gate.error": {
        "ru": "⚠️ Ошибка проверки: <code>{detail}</code>",
        "en": "⚠️ Check error: <code>{detail}</code>",
    },
    "gate.bot_not_admin": {
        "ru": "Бот не может проверить подписку: добавь бота в канал "
        "{channel} как администратора.",
        "en": "Bot can't verify subscription: add the bot to the "
        "{channel} channel as an administrator.",
    },
    "need_project": {
        "ru": "Сначала выбери проект: /start",
        "en": "Pick a project first: /start",
    },
    "busy_processing": {
        "ru": "Уже обрабатываю твой предыдущий запрос, подожди…",
        "en": "Still processing your previous request, please wait…",
    },
    # Project screen
    "project.limit": {
        "ru": "Лимит: <b>{ok}</b> / {limit} (осталось {left})",
        "en": "Limit: <b>{ok}</b> / {limit} ({left} left)",
    },
    "project.proxy_own": {
        "ru": "🌐 Прокси: свои ({n})",
        "en": "🌐 Proxies: own ({n})",
    },
    # Limits exhausted
    "limit.exhausted": {
        "ru": "Лимит для <b>{name}</b> исчерпан: <b>{limit}</b> успешных.{extra}",
        "en": "Limit for <b>{name}</b> reached: <b>{limit}</b> successful.{extra}",
    },
    "limit.exhausted_zero": {
        "ru": "У тебя 0 слотов в <b>{name}</b>. Лимит: {limit}.",
        "en": "You have 0 slots left in <b>{name}</b>. Limit: {limit}.",
    },
    # Submit flow
    "submit.sending": {
        "ru": (
            "Отправляю в <b>{name}</b>: <code>{handle}</code>…\n"
            "Слотов: {left} / {limit}\n"
            "<i>Если сервер занят — бот сам повторит (до {retries} раз).</i>"
        ),
        "en": (
            "Submitting to <b>{name}</b>: <code>{handle}</code>…\n"
            "Slots: {left} / {limit}\n"
            "<i>If the server is busy, the bot will retry (up to {retries} times).</i>"
        ),
    },
    "submit.error": {
        "ru": "Ошибка сабмита: <code>{err}</code>",
        "en": "Submit error: <code>{err}</code>",
    },
    "submit.bad_format": {
        "ru": "Не понял формат.\n\n{help}",
        "en": "Couldn't parse that format.\n\n{help}",
    },
    # Result messages
    "result.ok": {
        "ru": (
            "✅ <b>Принято</b> · {name}\n"
            "handle: <code>{handle}</code>\n"
            "wallet: <code>{wallet}</code>\n"
            "Осталось слотов: <b>{left}</b> из {limit}"
        ),
        "en": (
            "✅ <b>Accepted</b> · {name}\n"
            "handle: <code>{handle}</code>\n"
            "wallet: <code>{wallet}</code>\n"
            "Slots left: <b>{left}</b> of {limit}"
        ),
    },
    "result.already": {
        "ru": (
            "ℹ️ <b>Уже зарегистрировано</b> · {name}\n"
            "handle: <code>{handle}</code>\n"
            "Этот wallet/handle уже в whitelist. Слот <b>не</b> списан.\n"
            "Успешных: {ok} / {limit}"
        ),
        "en": (
            "ℹ️ <b>Already registered</b> · {name}\n"
            "handle: <code>{handle}</code>\n"
            "This wallet/handle is already on the whitelist. Slot <b>not</b> used.\n"
            "Successful: {ok} / {limit}"
        ),
    },
    "result.busy": {
        "ru": (
            "⏳ <b>Сервер занят</b> · {name}\n"
            "Google Sheet временно locked (много заявок сразу).\n"
            "Подожди <b>1–2 минуты</b> и отправь <b>ту же строку ещё раз</b>.\n"
            "Слот <b>не</b> списан.\n"
            "Успешных: {ok} / {limit}"
        ),
        "en": (
            "⏳ <b>Server busy</b> · {name}\n"
            "Google Sheet is temporarily locked (many submissions at once).\n"
            "Wait <b>1–2 minutes</b> and send <b>the same line again</b>.\n"
            "Slot <b>not</b> used.\n"
            "Successful: {ok} / {limit}"
        ),
    },
    "result.bad": {
        "ru": (
            "⚠️ <b>Сервер не принял</b> · {name}\n"
            "{reason}\n"
            "Слот <b>не</b> списан — можно исправить и повторить.\n"
            "Успешных: {ok} / {limit}"
        ),
        "en": (
            "⚠️ <b>Rejected by server</b> · {name}\n"
            "{reason}\n"
            "Slot <b>not</b> used — fix and try again.\n"
            "Successful: {ok} / {limit}"
        ),
    },
    "result.fail": {
        "ru": (
            "❌ <b>Сеть / прокси</b> · {name}\n"
            "Не удалось достучаться до сервера. Слот <b>не</b> списан.\n"
            "Повтори через минуту или проверь /proxy.\n"
            "Успешных: {ok} / {limit}"
        ),
        "en": (
            "❌ <b>Network / proxy</b> · {name}\n"
            "Could not reach the server. Slot <b>not</b> used.\n"
            "Retry in a minute or check /proxy.\n"
            "Successful: {ok} / {limit}"
        ),
    },
    # Friendly BAD_RESP reasons
    "reason.invalid_twitter": {
        "ru": "Некорректный Twitter/X handle.",
        "en": "Invalid Twitter/X handle.",
    },
    "reason.invalid_wallet": {
        "ru": "Некорректный wallet (нужен 0x + 40 hex).",
        "en": "Invalid wallet (need 0x + 40 hex).",
    },
    "reason.duplicate": {
        "ru": "Такая запись уже есть в whitelist.",
        "en": "This entry is already on the whitelist.",
    },
    "reason.invalid_link": {
        "ru": "Некорректная ссылка (нужен post/status URL).",
        "en": "Invalid link (need a post/status URL).",
    },
    "reason.generic": {
        "ru": "Сервер отклонил заявку.",
        "en": "The server rejected the submission.",
    },
    # Human-readable failures (bot/errors.py). No HTTP codes, no raw bodies.
    "error.network": {
        "ru": "Не удалось достучаться до сайта проекта — сеть или прокси. Слот не списан.",
        "en": "Could not reach the project site — network or proxy. No slot used.",
    },
    "error.timeout": {
        "ru": "Сайт проекта не ответил вовремя. Попробуй ещё раз через минуту.",
        "en": "The project site did not answer in time. Try again in a minute.",
    },
    "error.captcha": {
        "ru": "Проект требует капчу, и решить её не удалось.",
        "en": "The project asks for a captcha and it could not be solved.",
    },
    "error.closed": {
        "ru": "Регистрация в этом проекте закрыта.",
        "en": "Registration for this project is closed.",
    },
    "error.rate_limited": {
        "ru": "Проект временно ограничил частоту заявок. Подожди пару минут.",
        "en": "The project is rate-limiting submissions. Wait a couple of minutes.",
    },
    "error.busy": {
        "ru": "Таблица проекта занята другими заявками. Повтори через 1–2 минуты.",
        "en": "The project sheet is busy with other submissions. Retry in 1–2 minutes.",
    },
    "error.server": {
        "ru": "На стороне проекта сбой. Слот не списан, попробуй позже.",
        "en": "The project side is failing. No slot used, try later.",
    },
    "error.invalid_handle": {
        "ru": "Проект не принял X-аккаунт: проверь имя пользователя.",
        "en": "The project rejected the X account: check the username.",
    },
    "error.invalid_wallet": {
        "ru": "Проект не принял кошелёк: нужен адрес 0x + 40 символов.",
        "en": "The project rejected the wallet: it needs 0x + 40 characters.",
    },
    "error.invalid_link": {
        "ru": "Проект не принял ссылку: нужна ссылка на пост.",
        "en": "The project rejected the link: it needs a post URL.",
    },
    "error.duplicate": {
        "ru": "Эта запись уже есть в whitelist. Слот не списан.",
        "en": "This entry is already on the whitelist. No slot used.",
    },
    "error.not_eligible": {
        "ru": "Этот кошелёк не eligible в этом проекте — аллокации нет.",
        "en": "This wallet is not eligible for this project — no allocation.",
    },
    "error.no_proxies": {
        "ru": "Нет рабочих прокси. Загрузи свои в «🌐 Прокси» или попробуй позже.",
        "en": "No working proxies. Upload your own in «🌐 Proxies» or try later.",
    },
    "error.generic": {
        "ru": "Что-то пошло не так. Код: <code>{code}</code>",
        "en": "Something went wrong. Code: <code>{code}</code>",
    },
    # /status /limit
    "status.title": {
        "ru": "<b>Лимиты по проектам</b>:",
        "en": "<b>Limits by project</b>:",
    },
    "status.none": {
        "ru": "Нет активных проектов.",
        "en": "No active projects.",
    },
    "status.line": {
        "ru": "• <b>{name}</b>: {ok} / {limit} (осталось {left}){mark}",
        "en": "• <b>{name}</b>: {ok} / {limit} ({left} left){mark}",
    },
    "status.selected": {"ru": " ← выбран", "en": " ← selected"},
    "status.stale": {
        "ru": "Ранее выбран «{id}» — проект недоступен, выбери /start.",
        "en": "Previously selected «{id}» is unavailable — pick again via /start.",
    },
    # Proxy
    "proxy.title": {"ru": "<b>🌐 Прокси</b>", "en": "<b>🌐 Proxies</b>"},
    "proxy.vip_own": {
        "ru": "Сейчас: <b>свои</b> · {n} шт.\nЛимит проектов: <b>{limit}</b> (VIP)",
        "en": "Now: <b>own</b> · {n}\nProject limit: <b>{limit}</b> (VIP)",
    },
    "proxy.vip_shared": {
        "ru": "Сейчас: <b>общие</b> ({n} в proxy.txt)\nЛимит проектов: <b>{limit}</b> (VIP)",
        "en": "Now: <b>shared</b> ({n} in proxy.txt)\nProject limit: <b>{limit}</b> (VIP)",
    },
    "proxy.vip_note": {
        "ru": "Можно без своих: используются общие. Свои — опционально.",
        "en": "Own proxies are optional; the shared pool is used otherwise.",
    },
    "proxy.vip_note_own_only": {
        "ru": "Общий пул выключен — без своих прокси регистрации не пойдут.",
        "en": "The shared pool is off — without your own proxies nothing will submit.",
    },
    "proxy.shared_off": {
        "ru": "Сейчас: общий пул <b>выключен</b>. Регистрации только через свои прокси.",
        "en": "Now: shared pool is <b>off</b>. Registrations only go through your own proxies.",
    },
    "proxy.own": {
        "ru": "Сейчас: <b>свои</b> · {n} шт.\nРегистрации идут через них.",
        "en": "Now: <b>own</b> · {n}\nRegistrations go through them.",
    },
    "proxy.shared": {
        "ru": "Сейчас: <b>общие</b> ({n} в proxy.txt) · лимит проектов <b>{free}</b>",
        "en": "Now: <b>shared</b> ({n} in proxy.txt) · project limit <b>{free}</b>",
    },
    "proxy.own_tip": {
        "ru": "Можно загрузить свои прокси — регистрации пойдут через них.",
        "en": "You can upload your own proxies — registrations will go through them.",
    },
    "proxy.health": {
        "ru": "🩺 За 24 ч: прокси-сбои в {fails_proxy} из {rows} строк (ошибок всего: {fails})",
        "en": "🩺 Last 24h: proxy transport failures in {fails_proxy} of {rows} rows (total errors: {fails})",
    },
    "btn.proxy_check": {
        "ru": "🧪 Проверить прокси",
        "en": "🧪 Test proxies",
    },
    "proxy.check_running": {
        "ru": "Проверяю первые {n} прокси, это займёт до минуты…",
        "en": "Testing the first {n} proxies, up to a minute…",
    },
    "proxy.check_cooldown": {
        "ru": "Проверку можно запускать раз в 5 минут — подожди {sec} c.",
        "en": "Checks are limited to one per 5 minutes — wait {sec}s.",
    },
    "proxy.check_none": {
        "ru": "Сначала загрузи свои прокси — проверять нечего.",
        "en": "Upload your own proxies first — nothing to test.",
    },
    "proxy.check_report": {
        "ru": "🧪 <b>Проверка прокси</b>: живых {alive} из {checked}",
        "en": "🧪 <b>Proxy check</b>: {alive} of {checked} alive",
    },
    "proxy.check_prefix": {
        "ru": "первые {checked} из {total} строк файла",
        "en": "first {checked} of {total} file lines",
    },
    "proxy.howto": {
        "ru": (
            "Пришли <b>.txt</b> (заменит список целиком):\n"
            "<code>host:port:user:pass</code>\n"
            "<code>socks5:host:port:user:pass</code>\n\n"
            "До <b>{max_lines}</b> строк, файл ≤ <b>{max_kb} KB</b>.\n"
            "Ошибка в любой строке → файл <b>не</b> принят."
        ),
        "en": (
            "Send a <b>.txt</b> (replaces the whole list):\n"
            "<code>host:port:user:pass</code>\n"
            "<code>socks5:host:port:user:pass</code>\n\n"
            "Up to <b>{max_lines}</b> lines, file ≤ <b>{max_kb} KB</b>.\n"
            "Any bad line → file is <b>rejected</b>."
        ),
    },
    "proxy.delete": {"ru": "🗑 Удалить мои прокси", "en": "🗑 Delete my proxies"},
    "proxy.error.format": {
        "ru": "нужно host:port:user:pass (или socks5:host:port:user:pass)",
        "en": "expected host:port:user:pass (or socks5:host:port:user:pass)",
    },
    "proxy.error.required": {
        "ru": "пустые host/port/user/pass (пароль обязателен)",
        "en": "host/port/user/pass cannot be empty (password is required)",
    },
    "proxy.error.port": {
        "ru": "некорректный порт {port}",
        "en": "invalid port {port}",
    },
    "proxy.error.too_many": {
        "ru": "больше {max} строк — разбей файл",
        "en": "more than {max} lines — split the file",
    },
    "proxy.error.no_rows": {"ru": "нет строк с прокси", "en": "no proxy rows"},
    "proxy.error.empty_file": {"ru": "файл пустой", "en": "the file is empty"},
    "proxy.error.too_big": {
        "ru": "файл слишком большой (лимит {kb} KB)",
        "en": "file is too large (limit {kb} KB)",
    },
    "proxy.error.encoding": {
        "ru": "не удалось прочитать кодировку (UTF-8)",
        "en": "could not decode the file (UTF-8)",
    },
    "proxy.error.rejected": {
        "ru": "Файл не принят. Старый список без изменений.",
        "en": "File rejected. The previous list was not changed.",
    },
    "proxy.error.more": {"ru": "… и ещё {n}", "en": "… and {n} more"},
    "proxy.saved": {"ru": "Сохранено: {n} прокси", "en": "Saved: {n} proxies"},
    "proxy.confirm_clear": {
        "ru": "Точно удалить свои прокси? Регистрации снова пойдут через общий пул.",
        "en": "Delete your proxies? Registrations will use the shared pool again.",
    },
    "proxy.confirm_clear_own_only": {
        "ru": "Точно удалить свои прокси? Общий пул выключен — без них регистрации остановятся.",
        "en": "Delete your proxies? The shared pool is off — without them registrations stop.",
    },
    "proxy.clear_yes": {"ru": "Да, удалить", "en": "Yes, delete"},
    "proxy.removed": {"ru": "✅ Свои прокси удалены.", "en": "✅ Own proxies deleted."},
    "proxy.none_own": {
        "ru": "Своих прокси и не было — сабмиты идут через общий пул.",
        "en": "There were no own proxies — submissions use the shared pool.",
    },
    "proxy.none_own_off": {
        "ru": "Своих прокси и не было — общий пул выключен, регистрировать нечем.",
        "en": "There were no own proxies — the shared pool is off, nothing to submit with.",
    },
    # Every tier may upload proxies today (they are a traffic source, not a
    # tier). The key exists so a future policy change has a sentence instead
    # of a silent refusal.
    "proxy.upload_tier": {
        "ru": "Загрузка своих прокси недоступна на текущем тарифе.",
        "en": "Uploading your own proxies is not available on this plan.",
    },
    "proxy.upload_done": {
        "ru": "✅ {message}\nDNS check: {dns_ok}/{dns_total}\nСабмиты идут через <b>твои</b> прокси.",
        "en": "✅ {message}\nDNS check: {dns_ok}/{dns_total}\nSubmissions use <b>your</b> proxies.",
    },
    # Stats command
    "stats.title": {
        "ru": "<b>📊 Статистика</b>",
        "en": "<b>📊 Stats</b>",
    },
    "stats.line": {
        "ru": "• <b>{name}</b>: ✅ {ok}/{limit} · осталось {left}",
        "en": "• <b>{name}</b>: ✅ {ok}/{limit} · {left} left",
    },
    # New-project alert (bot/new_project_alert.py)
    "newproj.head": {
        "ru": "🆕 Новый проект: <b>{name}</b>",
        "en": "🆕 New project: <b>{name}</b>",
    },
    "newproj.mint": {"ru": "Минт: {info}", "en": "Mint: {info}"},
    "newproj.limit": {
        "ru": "Лимит: {free} · подписчикам {sub} · Basic {basic}",
        "en": "Limit: {free} · subscribers {sub} · Basic {basic}",
    },
    "newproj.links": {"ru": "Ссылки: {links}", "en": "Links: {links}"},
    "newproj.open_btn": {"ru": "▶️ Открыть проект", "en": "▶️ Open project"},
}


def normalize_lang(lang: str | None) -> str:
    l = (lang or DEFAULT_LANG).strip().lower()
    if l.startswith("en"):
        return "en"
    return "ru"


def t(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """Translate key for lang; missing key returns key name."""
    lang = normalize_lang(lang)
    block = STRINGS.get(key) or {}
    text = block.get(lang) or block.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError) as exc:
            # A placeholder mismatch must surface in the log, not as literal
            # braces in a user's chat.
            print(f"[i18n] format failed for {key} ({lang}): {exc}", flush=True)
            return text
    return text


def short_wallet(wallet: str | None, head: int = 6, tail: int = 4) -> str:
    w = (wallet or "").strip()
    if len(w) <= head + tail + 1:
        return w or "—"
    return f"{w[:head]}…{w[-tail:]}"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form selector (also works for Ukrainian/BY-style counts)."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def noun_accounts(n: int, lang: str | None = None) -> str:
    lang = normalize_lang(lang)
    if lang == "en":
        return "account" if abs(int(n)) == 1 else "accounts"
    return plural_ru(n, "аккаунт", "аккаунта", "аккаунтов")


def noun_registrations(n: int, lang: str | None = None, *, accusative: bool = False) -> str:
    """Russian noun for 'registration'. accusative=True after «запустить»."""
    lang = normalize_lang(lang)
    if lang == "en":
        return "registration" if abs(int(n)) == 1 else "registrations"
    if accusative:
        return plural_ru(n, "регистрацию", "регистрации", "регистраций")
    return plural_ru(n, "регистрация", "регистрации", "регистраций")


def status_emoji(status: str) -> str:
    return {
        "OK": "✅",
        "ALREADY": "ℹ️",
        "BAD_RESP": "⚠️",
        "FAIL": "❌",
        "BUSY": "⏳",
    }.get((status or "").upper(), "•")


def friendly_bad_reason(
    detail: str, lang: str | None = None, *, include_detail: bool = False
) -> str:
    """Map technical detail → short user-facing reason.

    ``include_detail`` appends the sanitized raw body; it is for the admin
    surface only. User-facing callers must leave it off — a trailing
    ``<code>{body}</code>`` was the main way raw responses leaked into chat.
    """
    d = (detail or "").lower()
    if any(x in d for x in ("duplicate", "already", "23505", "unique", "exists")):
        return t("reason.duplicate", lang)
    if "not eligible" in d or "no allocation" in d:
        return t("error.not_eligible", lang)
    if any(x in d for x in ("invalid twitter", "invalid handle", "twitter handle", "username")):
        return t("reason.invalid_twitter", lang)
    if any(x in d for x in ("invalid wallet", "wallet", "evm", "address")) and "proxy" not in d:
        if "invalid" in d or "bad" in d:
            return t("reason.invalid_wallet", lang)
    if any(x in d for x in ("retweet", "comment", "quote", "status", "link", "url")):
        if any(x in d for x in ("invalid", "required", "missing", "need")):
            return t("reason.invalid_link", lang)
    if include_detail:
        from bot.ux import safe_detail

        cleaned = safe_detail(detail, limit=120)
        if cleaned and cleaned not in ("—",):
            return f"{t('reason.generic', lang)} <code>{cleaned}</code>"
    return t("reason.generic", lang)
