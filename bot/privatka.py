"""Private-group upsell (приватка): copy and branches, no payments here.

Money and kicks live in an external bot (``UPSELL_BOT``). This bot only
reads group membership and points at ``t.me/<that bot>``. Strings are always
Russian. With ``UPSELL_BOT`` unset the whole feature hides.
"""
from __future__ import annotations

from telebot import types

from bot import billing
from bot import billing_ux
from bot import config
from bot import group_tier
from bot import limits as bot_limits
from bot import subscribers
from bot.i18n import t
from bot.ux import esc

LANG = "ru"


def bot_username() -> str:
    raw = str(getattr(config, "UPSELL_BOT", "") or "")
    return raw.strip().lstrip("@")


def enabled() -> bool:
    return bool(bot_username())


def pay_url() -> str:
    return f"https://t.me/{bot_username()}"


def _in_group(user_id: int) -> bool:
    try:
        return bool(group_tier.is_member(int(user_id)))
    except Exception:
        return False


def _in_file(user_id: int) -> bool:
    try:
        return bool(subscribers.is_subscriber(int(user_id)))
    except Exception:
        return False


def is_free_user(user_id: int) -> bool:
    """No VIP, no paid plan, no file, no group — the 5-OK public tier."""
    uid = int(user_id)
    if bot_limits.is_vip(uid):
        return False
    if billing.is_active(uid):
        return False
    if _in_group(uid) or _in_file(uid):
        return False
    return bot_limits.limit_mode(uid) == "free"


def last_done_job(user_id: int):
    """Most recent finished job row, or None. Lightweight fake for is_free_win."""
    from bot import db as bot_db

    try:
        with bot_db.db() as conn:
            row = conn.execute(
                "SELECT status, ok, total FROM submit_jobs "
                "WHERE user_id=? AND status='done' ORDER BY id DESC LIMIT 1",
                (int(user_id),),
            ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return type(
        "Job",
        (),
        {
            "user_id": int(user_id),
            "status": str(row["status"] or ""),
            "ok": int(row["ok"] or 0),
            "total": int(row["total"] or 0),
        },
    )()


def is_free_win(job) -> bool:
    """This run finished 5/5 OK on the free tier."""
    if job is None:
        return False
    if str(getattr(job, "status", "") or "") != "done":
        return False
    if int(getattr(job, "ok", 0) or 0) != 5:
        return False
    if int(getattr(job, "total", 0) or 0) != 5:
        return False
    try:
        return is_free_user(int(job.user_id))
    except Exception:
        return False


def is_basic_holder(user_id: int) -> bool:
    uid = int(user_id)
    if bot_limits.is_vip(uid) or _in_group(uid):
        return False
    return bool(billing.is_active(uid))


def basic_until_line(user_id: int) -> str:
    current = billing.active_plan(int(user_id))
    if current is None:
        return ""
    until = billing_ux._fmt_date(current[1], LANG)
    return t("priv.basic_line", LANG, date=esc(until))


def win_line() -> str:
    return t("priv.win_line", LANG)


def branch(user_id: int) -> str:
    """Which interstitial to draw: vip / group / basic / file / offer."""
    uid = int(user_id)
    if bot_limits.is_vip(uid):
        return "vip"
    if _in_group(uid):
        return "group"
    if billing.is_active(uid):
        return "basic"
    if _in_file(uid):
        return "file"
    return "offer"


def screen_text(user_id: int) -> str:
    kind = branch(user_id)
    name = bot_username()
    if kind == "vip":
        return t("priv.screen_vip", LANG)
    if kind == "group":
        return t("priv.screen_group", LANG)
    if kind == "basic":
        line = basic_until_line(user_id)
        return (line + "\n\n" if line else "") + t("priv.screen_offer", LANG, bot=name)
    if kind == "file":
        return t("priv.screen_file", LANG, bot=name)
    return t("priv.screen_offer", LANG, bot=name)


def _url_button(label_key: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text=t(label_key, LANG, bot=bot_username()),
        url=pay_url(),
    )


def screen_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    kind = branch(int(user_id))
    kb = types.InlineKeyboardMarkup()
    if kind in ("vip", "group"):
        kb.add(_url_button("priv.btn_manage"))
    else:
        kb.add(_url_button("priv.btn_pay"))
    kb.add(
        types.InlineKeyboardButton(
            text=t("btn.to_projects", LANG),
            callback_data="projects:back",
        )
    )
    return kb


def nav_button() -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text=t("priv.btn_nav", LANG),
        callback_data="priv:menu",
    )


def win_button() -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text=t("priv.btn_win", LANG),
        url=pay_url(),
    )


def after_job_line(job) -> str:
    """Extra line under a finished run, or empty."""
    if job is None or str(getattr(job, "status", "") or "") != "done":
        return ""
    uid = int(job.user_id)
    if is_free_win(job):
        return win_line()
    if is_basic_holder(uid):
        return basic_until_line(uid)
    return ""


def attach_after_job(text: str, markup, job):
    """Append the after-run pitch and, for a free 5/5, the pay button."""
    if not enabled():
        return text, markup
    extra = after_job_line(job)
    if extra:
        text = (text.rstrip() + "\n\n" + extra) if text else extra
    if is_free_win(job):
        kb = markup if markup is not None else types.InlineKeyboardMarkup()
        if not getattr(kb, "keyboard", None):
            kb = types.InlineKeyboardMarkup()
        kb.add(win_button())
        markup = kb
        try:
            from bot import db as bot_db

            bot_db.log_funnel_event(int(job.user_id), "win_shown")
        except Exception:
            pass
    return text, markup
