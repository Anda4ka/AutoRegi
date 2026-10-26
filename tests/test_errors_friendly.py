"""User-facing failure text: actionable, and free of raw server output."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="errors_test_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "test:token"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import errors as bot_errors  # noqa: E402
from bot import i18n  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# Real details taken from the project adapters in this repo.
SAMPLES: list[tuple[str, str, str]] = [
    ("FAIL", "proxy error: HTTPSConnectionPool(host='x') Max retries exceeded", "error.network"),
    ("FAIL", "Read timed out. (read timeout=30)", "error.timeout"),
    ("FAIL", "HoodRoost requires a proxy", "error.no_proxies"),
    ("FAIL", "own_proxy_invalid: file corrupt", "error.no_proxies"),
    ("BAD_RESP", "turnstile token missing", "error.captcha"),
    ("BAD_RESP", "HTTP 404: Not Found", "error.closed"),
    ("BAD_RESP", "registration closed", "error.closed"),
    ("BAD_RESP", '{"error":"wallet taken","duplicate":true}', "error.duplicate"),
    ("BAD_RESP", "invalid twitter handle", "error.invalid_handle"),
    ("BAD_RESP", "invalid wallet address", "error.invalid_wallet"),
    ("BAD_RESP", "not eligible for allocation", "error.not_eligible"),
    ("BAD_RESP", "invalid link: need a status url", "error.invalid_link"),
    ("BAD_RESP", "HTTP 429 too many requests", "error.rate_limited"),
    ("BAD_RESP", "Service invoked too many times ... LockService sheet lock", "error.busy"),
    ("FAIL", "HTTP 502 Bad Gateway", "error.server"),
]


def test_known_details_are_classified() -> None:
    for status, detail, expected in SAMPLES:
        got = bot_errors.classify(detail, status)
        _assert(got == expected, f"{detail!r} → {got}, expected {expected}")


def test_status_overrides_detail() -> None:
    _assert(bot_errors.classify("whatever", "ALREADY") == "error.duplicate", "ALREADY")
    _assert(bot_errors.classify("whatever", "BUSY") == "error.busy", "BUSY")


def test_user_message_hides_http_codes_and_bodies() -> None:
    leaky = [
        'HTTP 400: {"ok":false,"error":"bad handle","trace":"/api/apply"}',
        "HTTPSConnectionPool(host='api.example.com', port=443): Max retries exceeded",
        "Traceback (most recent call last): KeyError: 'wallet'",
        "<html><body>Script function not found: doPost</body></html>",
    ]
    for detail in leaky:
        for lang in ("ru", "en"):
            msg = bot_errors.user_message("BAD_RESP", detail, lang=lang)
            _assert("http" not in msg.lower(), f"HTTP leaked: {msg}")
            _assert("{" not in msg and "}" not in msg, f"JSON leaked: {msg}")
            _assert("traceback" not in msg.lower(), f"traceback leaked: {msg}")
            _assert("<html" not in msg.lower(), f"HTML leaked: {msg}")
            _assert(len(msg) <= 200, f"too long for a chat line: {msg}")


def test_every_reason_is_translated() -> None:
    keys = {key for _, key in bot_errors.REASONS} | {"error.generic"}
    for key in keys:
        for lang in ("ru", "en"):
            text = i18n.t(key, lang)
            _assert(text != key, f"missing translation for {key} [{lang}]")


def test_generic_fallback_carries_a_stable_code() -> None:
    detail = "something nobody has ever seen before"
    msg = bot_errors.user_message("BAD_RESP", detail, lang="ru")
    codes = re.findall(r"E-[0-9A-F]{6}", msg)
    _assert(codes, f"no code in {msg}")
    _assert(codes[0] == bot_errors.error_code(detail), "message code matches error_code")


def test_error_code_is_stable_and_discriminating() -> None:
    a = "HTTP 500 storage error on /api/register"
    b = "HTTP 500 storage error on /api/register"
    c = "completely different failure"
    _assert(bot_errors.error_code(a) == bot_errors.error_code(b), "same detail, same code")
    _assert(bot_errors.error_code(a) != bot_errors.error_code(c), "different detail, different code")
    # Digits are normalized so one failure does not get a new code per id.
    _assert(
        bot_errors.error_code("timeout after 30s on row 12")
        == bot_errors.error_code("timeout after 45s on row 998"),
        "numbers must not fragment the code",
    )
    _assert(bot_errors.error_code("") == "E-000000", "empty detail")


def test_admin_message_keeps_the_raw_detail() -> None:
    detail = 'HTTP 400: {"ok":false,"error":"bad handle"}'
    admin = bot_errors.admin_message("BAD_RESP", detail)
    _assert("bad handle" in admin, f"operator keeps the body: {admin}")
    user = bot_errors.user_message("BAD_RESP", detail)
    _assert("bad handle" not in user, f"user does not: {user}")


def test_friendly_bad_reason_drops_detail_by_default() -> None:
    detail = "duplicate wallet already registered in sheet ABC123"
    plain = i18n.friendly_bad_reason(detail, "ru")
    _assert("<code>" not in plain, f"no raw fragment by default: {plain}")
    verbose = i18n.friendly_bad_reason("weird failure xyz", "ru", include_detail=True)
    _assert("<code>" in verbose, f"admin opt-in still works: {verbose}")


def test_project_name_prefix() -> None:
    msg = bot_errors.user_message(
        "FAIL", "connection refused", lang="ru", project_name="Hood Citizens"
    )
    _assert(msg.startswith("Hood Citizens: "), msg)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
