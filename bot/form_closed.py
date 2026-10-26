"""Detect closed registration forms and alert admins (runtime disable)."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from bot import config
from bot import db as bot_db
from bot.ux import esc

_ALERT_LOCK = threading.Lock()

# Hours before re-alerting the same project after a form-closed signal.
DEDUP_HOURS = int(
    __import__("os").environ.get("ADMIN_FORM_CLOSED_DEDUP_HOURS", "6")
)

# EN/RU phrases that usually mean WL / apply is shut.
# Avoid bare "closed" alone when too noisy — prefer multi-word / common API strings.
_CLOSED_PHRASES = (
    "gate closed",
    "applications are closed",
    "application is closed",
    "applications closed",
    "registration closed",
    "registration is closed",
    "registrations closed",
    "reg closed",
    "whitelist closed",
    "wl closed",
    "allowlist closed",
    "allowlist is closed",
    "mint ended",
    "mint is over",
    "mint closed",
    "not accepting",
    "no longer accepting",
    # Google only ever serves this path for a form that stopped accepting
    # responses, so it is a definite signal and — unlike the prose above —
    # survives a response localized by the proxy's Accept-Language.
    "closedform",
    "submissions closed",
    "submit closed",
    "form closed",
    "form is closed",
    "entry closed",
    "entries closed",
    "signup closed",
    "sign-up closed",
    # WeZards' own wording for `applicationEnabled: false`.
    "applications are currently paused",
    "applications are paused",
    "closed for registration",
    "closed for applications",
    "project is closed",
    "apply closed",
    "apply is closed",
    "регистрация закрыта",
    "регистрация закрыт",
    "набор закрыт",
    "форма закрыта",
    "прием закрыт",
    "приём закрыт",
    "закрыт набор",
    "закрыта регистрация",
)

# Single-token / short signals that APIs return as error field
_CLOSED_EXACT = frozenset(
    {
        "closed",
        "gate closed",
        "registration_closed",
        "reg_closed",
        "wl_closed",
        "allowlist_closed",
        "applications_closed",
        "not_accepting",
    }
)

# Duplicates / already-registered — never treat as form closed
_ALREADY_MARKERS = (
    "wallet taken",
    "already been submitted",
    "already submitted",
    "already registered",
    "already applied",
    "already exists",
    "duplicate",
)

# A removed primary submit endpoint is operationally equivalent to a closed
# form. Require an explicit HTTP/status prefix: bare "not found" can describe
# an invalid X handle and must not disable an otherwise healthy project.
_UNAVAILABLE_HTTP_RE = re.compile(
    r"(?:\bhttp\s*|\bstatus\s*[:=]?\s*)(?:404|410)(?:\b|\D)",
    re.IGNORECASE,
)

# An Apps Script deployment that still answers but whose script version defines
# no doPost is gone in every way that matters: it 200s a localized error page
# and writes nothing. Adapters translate that page into this English phrase, so
# matching it here needs no locale guesswork.
_ENDPOINT_GONE_MARKERS = (
    "script function not found",
    # The gform adapter's stable phrase for a login-walled Google form:
    # the endpoint answers 401 forever, which is endpoint-gone in every
    # way that matters (gftestform, 700 rows, 2026-09-24).
    "форма требует вход в google",
)

# A supply that ran out is a closed form that never says "closed". robidrop
# answered `409 {"error":"Series is full — every pass has been claimed."}` to
# 1876 rows across a full day and raised nothing, because none of the phrases
# above appear in it; hoodroost's `"Applications are closed. The roost is full."`
# only alerted on the first half of the sentence. These patterns are kept
# deliberately two-worded — a bare "full" shows up in transient plumbing errors
# ("queue is full") that must not read as a dead project. A false positive here
# costs one admin notification with a «hide project» button, never an automatic
# outage, so the balance favours catching the sell-out.
_SOLD_OUT_RE = re.compile(
    r"\b(?:is|are|was|were)\s+full\b"
    r"|\bsold\s*out\b"
    r"|\b(?:has|have)\s+(?:all\s+)?been\s+claimed\b"
    r"|\bno\s+(?:spots?|places?|slots?|passes)\s+(?:left|remaining)\b"
    r"|\bfully\s+(?:claimed|allocated|subscribed)\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    s = (text or "").strip().lower()
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def is_form_closed_detail(
    detail: str | None,
    status: str | None = None,
    *,
    data: dict | None = None,
) -> bool:
    """
    True when response looks like permanent form/WL closed (not ALREADY/OK).

    status OK/ALREADY/BUSY never closed. Works on BAD_RESP and FAIL bodies
    that carry explicit closed text.
    """
    st = (status or "").strip().upper()
    if st in ("OK", "ALREADY", "BUSY"):
        return False

    # Proxy noise is never a closed form. A block page carries the endpoint's
    # own HTTP code (404 on script.google.com seen 2026-09-16), so the
    # _UNAVAILABLE_HTTP_RE below would read routine pool churn as "project
    # gone" and page admins about a working project.
    from bot.http_client import is_proxy_block_page_detail, is_proxy_fail_detail

    detail_text = (detail or "").strip()
    if is_proxy_fail_detail(detail_text) or is_proxy_block_page_detail(detail_text):
        return False

    chunks: list[str] = []
    if detail:
        chunks.append(str(detail))
    if isinstance(data, dict):
        for k in ("error", "message", "detail", "reason", "status"):
            v = data.get(k)
            if v is not None:
                chunks.append(str(v))

    blob = _norm(" ".join(chunks))
    if not blob:
        return False

    # Pure duplicate signals (even if body also mentions "closed")
    if any(x in blob for x in _ALREADY_MARKERS):
        return False
    # bare "already" but not "already closed"
    if re.search(r"\balready\b", blob) and "closed" not in blob:
        return False

    if _UNAVAILABLE_HTTP_RE.search(blob):
        return True

    if any(marker in blob for marker in _ENDPOINT_GONE_MARKERS):
        return True

    if _SOLD_OUT_RE.search(blob):
        return True

    compact = blob.replace(" ", "")
    if blob in _CLOSED_EXACT or blob.strip("\"'") in _CLOSED_EXACT:
        return True
    for exact in _CLOSED_EXACT:
        ex = exact.replace(" ", "")
        if f'"error":"{ex}"' in compact or f"'error':'{ex}'" in compact:
            return True

    for phrase in _CLOSED_PHRASES:
        if phrase in blob:
            return True

    # "closed" + domain word (application/registration/…)
    if re.search(r"\bclosed\b", blob) and any(
        x in blob
        for x in (
            "application",
            "registration",
            "whitelist",
            "allowlist",
            "form",
            "submit",
            "signup",
            "sign-up",
            "mint",
            "gate",
            "entry",
        )
    ):
        return True

    return False


def _alert_key(project_id: str) -> str:
    return f"form_closed:{project_id}"


def should_send_form_closed_alert(project_id: str) -> bool:
    prev = bot_db.get_alert_marker(_alert_key(project_id))
    if not prev:
        return True
    try:
        ts = datetime.fromisoformat(prev)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts >= timedelta(hours=DEDUP_HOURS)
    except Exception:
        return True


def mark_form_closed_alert_sent(project_id: str) -> None:
    bot_db.set_alert_marker(
        _alert_key(project_id),
        datetime.now(timezone.utc).isoformat(),
    )


def form_closed_alert_text(
    project_id: str,
    project_name: str,
    *,
    detail: str = "",
    status: str = "",
) -> str:
    snippet = esc((detail or "")[:200])
    return (
        f"🚨 <b>Проект перестал принимать заявки?</b>\n\n"
        f"Проект: <b>{esc(project_name)}</b> "
        f"(<code>{esc(project_id)}</code>)\n"
        f"Ответ: <code>{esc(status or '?')}</code>\n"
        f"Причина: <code>{snippet or '—'}</code>\n\n"
        f"Если регистрация действительно закрыта, скройте проект кнопкой ниже. "
        f"Текущие задания этого проекта будут аккуратно остановлены."
    )


def form_closed_alert_markup(project_id: str):
    """Inline keyboard: disable project + open admin projects."""
    from telebot import types

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "⏸ Скрыть проект",
            callback_data=f"admin:proj_off:{project_id}",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "🗂 Управление проектами",
            callback_data="admin:projects",
        )
    )
    return kb


def maybe_alert_form_closed(
    *,
    project_id: str,
    project_name: str,
    status: str,
    detail: str,
    send: Callable[[int, str, Any], None] | None = None,
    bot: Any = None,
) -> bool:
    """
    If detail looks closed and dedup allows, notify each ADMIN_IDS.

    send(admin_id, text, reply_markup) optional; else bot.send_message.
    Returns True if at least one alert was attempted.
    """
    if not is_form_closed_detail(detail, status):
        return False
    if not config.ADMIN_IDS:
        return False
    with _ALERT_LOCK:
        if not should_send_form_closed_alert(project_id):
            return False

        text = form_closed_alert_text(
            project_id, project_name, detail=detail, status=status
        )
        markup = form_closed_alert_markup(project_id)

        sent_any = False
        for aid in config.ADMIN_IDS:
            try:
                if send is not None:
                    send(int(aid), text, markup)
                elif bot is not None:
                    bot.send_message(
                        int(aid),
                        text,
                        parse_mode="HTML",
                        reply_markup=markup,
                        disable_web_page_preview=True,
                    )
                else:
                    continue
                sent_any = True
            except Exception as e:
                print(f"[form_closed] alert admin={aid} err={e}", flush=True)
        # Failed delivery must not suppress the next attempt for six hours.
        if sent_any:
            mark_form_closed_alert_sent(project_id)
            print(
                f"[form_closed] alert project={project_id} status={status} "
                f"detail={(detail or '')[:80]!r}",
                flush=True,
            )
        return sent_any
