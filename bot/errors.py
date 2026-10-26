"""Technical failure detail → one short sentence a user can act on.

Raw bodies, stack traces and HTTP codes are operator data: they scare users,
leak endpoint internals and say nothing about what to do next. Everything the
user sees comes from here; the unabridged ``detail`` stays in the submissions
table, the log and the admin panel.

When nothing matches, the user still gets a stable short code (``E-4F2A9C``)
that the operator can grep for in the log — support stays possible without
showing anyone a JSON blob.
"""

from __future__ import annotations

import hashlib
import re

from bot.i18n import t
from bot.ux import safe_detail


# Ordered: the first pattern that matches wins, so specific beats generic.
REASONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"no_proxies|own_proxy_invalid|requires a proxy"), "error.no_proxies"),
    (re.compile(r"captcha|turnstile|capsolver|2captcha|hcaptcha"), "error.captcha"),
    (
        re.compile(
            r"registration closed|whitelist closed|allowlist closed|form closed"
            r"|gate closed|http (?:404|410)|no longer available"
        ),
        "error.closed",
    ),
    (
        re.compile(r"duplicate|already (?:registered|applied|exists)|unique|23505"),
        "error.duplicate",
    ),
    (re.compile(r"not eligible|no allocation"), "error.not_eligible"),
    (re.compile(r"lock(?:ed)?service|sheet.*lock|lock.*sheet|too busy"), "error.busy"),
    (re.compile(r"http 429|rate.?limit|too many requests|slow down"), "error.rate_limited"),
    (
        re.compile(r"invalid (?:twitter|handle|username|x handle)|handle not found"),
        "error.invalid_handle",
    ),
    (
        re.compile(r"invalid (?:wallet|address|evm)|bad wallet|wallet.*(?:invalid|required)"),
        "error.invalid_wallet",
    ),
    (
        re.compile(
            r"invalid (?:link|url|post|tweet|comment|quote)"
            r"|(?:link|url|post url|status url).*(?:invalid|required|missing)"
        ),
        "error.invalid_link",
    ),
    (re.compile(r"timed? ?out|timeout"), "error.timeout"),
    (
        re.compile(
            r"proxy|tunnel|connection (?:reset|refused|aborted|error)|max retries"
            r"|ssl|certificate|http 407|econnreset|name or service not known"
        ),
        "error.network",
    ),
    (
        re.compile(r"http (?:500|502|503|504)|internal server error|bad gateway|storage error"),
        "error.server",
    ),
)

# Statuses that already carry their own meaning, whatever the detail says.
_STATUS_REASON = {
    "ALREADY": "error.duplicate",
    "BUSY": "error.busy",
}


def classify(detail: str, status: str = "") -> str:
    """Return the i18n key describing why a submission failed."""
    forced = _STATUS_REASON.get(str(status or "").upper())
    if forced:
        return forced
    text = (detail or "").lower()
    if not text.strip():
        return "error.generic"
    for pattern, key in REASONS:
        if pattern.search(text):
            return key
    return "error.generic"


def error_code(detail: str) -> str:
    """Short stable id for one kind of failure — greppable in the log."""
    normalized = re.sub(r"\d+", "#", (detail or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return "E-000000"
    digest = hashlib.blake2s(normalized.encode("utf-8"), digest_size=4).hexdigest()
    return f"E-{digest[:6].upper()}"


def user_message(
    status: str, detail: str, *, lang: str | None = None, project_name: str = ""
) -> str:
    """One actionable sentence. Never contains a raw body or an HTTP code."""
    key = classify(detail, status)
    if key == "error.generic":
        text = t("error.generic", lang, code=error_code(detail))
    else:
        text = t(key, lang)
    if project_name:
        return f"{project_name}: {text}"
    return text


def admin_message(status: str, detail: str, *, lang: str | None = None) -> str:
    """Same classification, plus the sanitized raw detail for the operator."""
    head = user_message(status, detail, lang=lang)
    cleaned = safe_detail(detail, limit=200)
    if cleaned and cleaned != "—":
        return f"{head}\n<code>{cleaned}</code>"
    return head
