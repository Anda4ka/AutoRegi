"""Tell users when a project appears in the bot — once, and never by surprise.

The trigger is deliberately the registry, not the outside world: an id shows up
in ``list_projects()`` only after an adapter has been written, enabled and
loaded, so «new project» here means «new project you can actually submit to».

Three things keep this from turning into a spam cannon:

* **Seeding.** The first run records the projects that already exist and sends
  nothing. Without it, switching this on would fire one alert per live project
  at every user in the same second.
* **A marker per project id** (``alert_new_project:<id>``, the same mechanism
  ``bot/form_closed.py`` uses). A ``/reload``, a service restart or a project
  briefly toggled off cannot re-announce what was already announced.
* **An admin holds the trigger.** Detection only drafts the announcement and
  shows it to the admins; nothing reaches a user until someone taps «Разослать».
  A blast to the whole base cannot be recalled, so it costs one tap.

Delivery is paced at :data:`DEFAULT_INTERVAL_SEC` seconds per user, best tier
first, so the project's own queue is not hit by everyone at once — see
``bot/broadcast.py``.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from bot import billing
from bot import broadcast
from bot import db as bot_db
from bot import group_tier
from bot.i18n import normalize_lang, t
from bot.ux import esc

# Ids the bot has already seen, comma separated, in bot_settings.
SETTING_KNOWN = "known_project_ids"
MARKER_PREFIX = "new_project:"

# One user per 10 seconds: the point is not politeness to Telegram (the global
# floor already handles that) but the project's own intake — 130 people opening
# the same whitelist in one minute is a queue nobody wins.
DEFAULT_INTERVAL_SEC = float(os.environ.get("NEW_PROJECT_ALERT_INTERVAL", "10"))
# Who gets it: people who actually submitted something recently.
ACTIVE_DAYS = int(os.environ.get("NEW_PROJECT_ALERT_ACTIVE_DAYS", "30"))


def is_enabled() -> bool:
    """Env kill-switch first, then the admin toggle in SQLite."""
    if os.environ.get("NEW_PROJECT_ALERTS", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return False
    return bot_db.get_setting("new_project_alerts_off", "") != "1"


def set_enabled(on: bool, *, admin_id: int | None = None) -> None:
    bot_db.set_setting(
        "new_project_alerts_off", "0" if on else "1", updated_by=admin_id
    )


def _known_ids() -> set[str]:
    raw = bot_db.get_setting(SETTING_KNOWN, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _store_known(ids: set[str]) -> None:
    bot_db.set_setting(SETTING_KNOWN, ",".join(sorted(ids)))


def _marker(project_id: str) -> str:
    return f"{MARKER_PREFIX}{project_id}"


# Serialises detect → preview-delivery → mark across the 30 s source watcher
# and a parallel /reload: without it both could see the same fresh id and
# deliver the preview twice before either marks it.
_NOTIFY_LOCK = threading.Lock()


def notify_lock():
    """Context manager around the whole detect-and-offer sequence."""
    return _NOTIFY_LOCK


def seed_if_empty(current_ids: list[str]) -> bool:
    """Record today's projects without announcing them. True when it seeded."""
    if _known_ids():
        return False
    _store_known({str(pid).strip() for pid in current_ids if str(pid).strip()})
    return True


def detect_new(current_ids: list[str]) -> list[str]:
    """Ids never seen and never announced. Empty on the seeding run."""
    current = [str(pid).strip() for pid in current_ids if str(pid).strip()]
    if seed_if_empty(current):
        return []
    known = _known_ids()
    fresh = []
    for pid in current:
        if pid in known:
            continue
        if bot_db.get_alert_marker(_marker(pid)):
            # Announced before and later dropped from `known` somehow — the
            # marker is the last word, otherwise a re-add would re-blast.
            continue
        fresh.append(pid)
    return fresh


def mark_announced(project_id: str) -> None:
    """Never offer this id again, whether it was sent or dismissed."""
    pid = str(project_id).strip()
    if not pid:
        return
    bot_db.set_alert_marker(_marker(pid), datetime.now(timezone.utc).isoformat())
    _store_known(_known_ids() | {pid})


def mint_info(project, lang: str) -> str:
    ru = str(getattr(project, "mint_info", "") or "").strip()
    en = str(getattr(project, "mint_info_en", "") or "").strip()
    if normalize_lang(lang) == "en":
        return en or ru
    return ru or en


def render(project, lang: str) -> str:
    """The announcement one user sees, in their language."""
    lang = normalize_lang(lang)
    name = esc(str(getattr(project, "name", "") or getattr(project, "id", "")))
    lines = [t("newproj.head", lang, name=name)]

    info = mint_info(project, lang)
    if info:
        lines.append(t("newproj.mint", lang, info=esc(info)))

    lines.append(
        t(
            "newproj.limit",
            lang,
            free=bot_db.get_free_limit(),
            sub=group_tier.subscriber_ok_limit(),
            basic=billing.plan_ok_limit("basic"),
        )
    )

    links = []
    site = str(getattr(project, "link", "") or "").strip()
    twitter = str(getattr(project, "twitter_url", "") or "").strip()
    if site:
        lines_site = esc(site.split("//", 1)[-1].rstrip("/"))
        links.append(f'<a href="{esc(site)}">{lines_site}</a>')
    if twitter:
        handle = esc(twitter.rstrip("/").rsplit("/", 1)[-1])
        links.append(f'<a href="{esc(twitter)}">@{handle}</a>')
    if links:
        lines.append(t("newproj.links", lang, links=" · ".join(links)))

    return "\n".join(lines)


def markup_json(project, lang: str) -> str:
    """One button, straight to the project's own screen."""
    return broadcast.build_markup_json(
        [
            [
                {
                    "text": t("newproj.open_btn", lang),
                    "callback_data": f"project:{getattr(project, 'id', '')}",
                }
            ]
        ]
    )


def batch_id(project_id: str) -> str:
    return f"newproj:{project_id}"


def audience() -> list[int]:
    return bot_db.list_active_user_ids(days=ACTIVE_DAYS)


def announce(project, *, interval_sec: float | None = None) -> tuple[str, int]:
    """Queue the announcement. Returns ``(batch_id, rows queued)``."""
    bid = batch_id(str(getattr(project, "id", "")))
    recipients = audience()
    langs: dict[int, str] = {}

    def _lang(uid: int) -> str:
        if uid not in langs:
            try:
                langs[uid] = bot_db.get_lang(uid)
            except Exception:
                langs[uid] = "ru"
        return langs[uid]

    queued = broadcast.enqueue(
        bid,
        recipients,
        body_for=lambda uid: render(project, _lang(uid)),
        markup_for=lambda uid: markup_json(project, _lang(uid)),
        parse_mode="HTML",
        interval_sec=(
            DEFAULT_INTERVAL_SEC if interval_sec is None else float(interval_sec)
        ),
    )
    return bid, queued


def preview_text(project, *, recipients: int, interval_sec: float | None = None) -> str:
    """What the admin approves: both languages, the audience and the runtime."""
    step = DEFAULT_INTERVAL_SEC if interval_sec is None else float(interval_sec)
    minutes = (recipients * step) / 60 if recipients else 0
    return (
        f"🆕 <b>Новый проект в реестре:</b> <code>{esc(str(getattr(project, 'id', '')))}</code>\n"
        f"Получателей: <b>{recipients}</b> (активные за {ACTIVE_DAYS} дн.)\n"
        f"Темп: 1 сообщение / {step:g} с → ~{minutes:.0f} мин, VIP первыми\n\n"
        "<b>RU:</b>\n"
        f"{render(project, 'ru')}\n\n"
        "<b>EN:</b>\n"
        f"{render(project, 'en')}"
    )
