"""Shared HTTP helpers for project modules (fingerprint, proxies, GAS helpers)."""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import random
import re
import tempfile
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import quote, urlparse

from requests.exceptions import ProxyError, ConnectTimeout, ConnectionError as ReqConnectionError


def log(msg: str) -> None:
    print(msg, flush=True)


ACCEPT_VALUES = [
    "*/*",
    "application/json, text/plain, */*",
    "*/*",
]



ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9", "en-US,en;q=0.8", "en-GB,en;q=0.9", "en-CA,en;q=0.9",
    "en-AU,en;q=0.9", "en-SG,en;q=0.9", "en,en-US;q=0.9", "en-US,en;q=0.9,ru;q=0.8",
    "en-US,en;q=0.9,de;q=0.7", "en-US,en;q=0.9,fr;q=0.8", "en-US,en;q=0.9,es;q=0.8",
    "en-US,en;q=0.9,ja;q=0.7", "en-US,en;q=0.9,zh;q=0.8", "en-US,en;q=0.9,ko;q=0.7",
    "en-US,en;q=0.9,pt;q=0.8", "en-US,en;q=0.9,it;q=0.8", "en-US,en;q=0.9,nl;q=0.7",
    "en-US,en;q=0.9,pl;q=0.7", "en-US,en;q=0.9,tr;q=0.7", "en-US,en;q=0.9,vi;q=0.8",
    "en-US,en;q=0.9,th;q=0.7", "en-US,en;q=0.9,id;q=0.8", "en-US,en;q=0.9,ar;q=0.7",
    "en-US,en;q=0.9,hi;q=0.7", "en-US,en;q=0.9,ms;q=0.7", "de-DE,de;q=0.9,en;q=0.8",
    "fr-FR,fr;q=0.9,en;q=0.8", "es-ES,es;q=0.9,en;q=0.8", "pt-BR,pt;q=0.9,en;q=0.8",
    "ja-JP,ja;q=0.9,en;q=0.8", "ko-KR,ko;q=0.9,en;q=0.8", "zh-CN,zh;q=0.9,en;q=0.8",
    "vi-VN,vi;q=0.9,en;q=0.8", "id-ID,id;q=0.9,en;q=0.8", "th-TH,th;q=0.9,en;q=0.8",
    "it-IT,it;q=0.9,en;q=0.8", "nl-NL,nl;q=0.9,en;q=0.8", "pl-PL,pl;q=0.9,en;q=0.8",
    "tr-TR,tr;q=0.9,en;q=0.8", "ar-SA,ar;q=0.9,en;q=0.8", "hi-IN,hi;q=0.9,en;q=0.8",
    "ms-MY,ms;q=0.9,en;q=0.8",
]

def _decodable_encodings() -> list[str]:
    """Content encodings this interpreter can actually decompress.

    Advertising an encoding we cannot decode is worse than looking slightly
    less like a browser: the server compresses, requests hands back raw bytes,
    and the adapter sees "HTTP 200 but not JSON" — with the payload right
    there, unreadable. brotli needs the `brotli` package; zstd needs urllib3
    with stdlib `compression.zstd` (Python 3.14+), which the `zstandard`
    package does NOT provide.
    """
    encodings = ["gzip", "deflate"]
    try:
        import brotli  # noqa: F401

        encodings.append("br")
    except ImportError:
        try:
            import brotlicffi  # noqa: F401

            encodings.append("br")
        except ImportError:
            pass
    try:
        from urllib3.response import HAS_ZSTD

        if HAS_ZSTD:
            encodings.append("zstd")
    except Exception:
        pass
    return encodings


DECODABLE_ENCODINGS = _decodable_encodings()
# Browsers vary; every variant here stays within what we can decode.
ACCEPT_ENCODINGS = sorted(
    {
        ", ".join(DECODABLE_ENCODINGS[:n])
        for n in range(2, len(DECODABLE_ENCODINGS) + 1)
    },
    key=len,
    reverse=True,
)
DEFAULT_ACCEPT_ENCODING = ACCEPT_ENCODINGS[0]

CHROME_MAJOR = ["131", "130", "129", "128", "127", "126", "125", "124", "123",
                "122", "121", "120", "119", "118", "117", "116", "115", "114", "113"]
FIREFOX_MAJOR = ["137", "136", "135", "134", "133", "132", "131", "130",
                 "129", "128", "127", "126", "125", "124", "123"]
EDGE_MAJOR = ["131", "130", "129", "128", "127", "126", "125", "124"]
SAFARI_MAJOR = ["18.3", "18.2", "18.1", "18.0", "17.6", "17.5", "17.4",
                "17.3", "17.2", "17.1", "17.0", "16.6", "16.5"]
IOS_PROFILES = [
    ("18_3", "18.3"), ("18_2", "18.2"), ("18_1", "18.1"), ("18_0", "18.0"),
    ("17_6", "17.6"), ("17_5", "17.5"), ("17_4", "17.4"), ("17_3", "17.3"),
    ("17_2", "17.2"), ("17_1", "17.1"), ("17_0", "17.0"),
    ("16_6", "16.6"), ("16_5", "16.5"),
]

WIN_NT = ["Windows NT 10.0"]
MAC_VER = ["10_15_7", "11_0", "12_0", "13_0", "14_0", "14_3", "14_4", "14_5", "15_0"]
ANDROID_VER = ["10", "11", "12", "13", "14", "15"]
LINUX_DIST = ["X11; Linux x86_64", "X11; Ubuntu; Linux x86_64", "X11; Fedora; Linux x86_64"]


def _chromium_full(major: str) -> str:
    return f"{major}.0.{random.randint(1000, 9999)}.{random.randint(10, 250)}"


def _base_headers(ua: str) -> dict:
    return {
        "User-Agent": ua,
        "Accept": random.choice(ACCEPT_VALUES),
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": random.choice(ACCEPT_ENCODINGS),
        # text/plain body avoids CORS preflight on many GAS deployments
        "Content-Type": "text/plain;charset=utf-8",
    }


def _chrome_headers(ua, major):
    h = _base_headers(ua)
    h.update({
        "sec-ch-ua": (
            f'"Google Chrome";v="{major}", "Chromium";v="{major}", '
            f'"Not?A_Brand";v="{random.choice(["24", "8", "99"])}"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": None,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
    })
    return h


def _firefox_headers(ua):
    return _base_headers(ua)


def _safari_headers(ua):
    h = _base_headers(ua)
    h["Accept-Encoding"] = DEFAULT_ACCEPT_ENCODING
    return h


def _edge_headers(ua, major):
    h = _base_headers(ua)
    h.update({
        "sec-ch-ua": (
            f'"Microsoft Edge";v="{major}", "Chromium";v="{major}", '
            f'"Not?A_Brand";v="{random.choice(["24", "8", "99"])}"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
    })
    return h


def _registrable(host: str | None) -> str:
    """Crude registrable domain: the last two labels (fine for same-site)."""
    if not host:
        return ""
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def _same_origin(origin: str | None, url: str | None) -> bool:
    """True when a fetch from ``origin`` to ``url`` is same-origin."""
    if not origin or not url:
        return False
    try:
        o, u = urlparse(origin), urlparse(url)
    except ValueError:
        return False
    return (o.scheme, o.hostname, o.port) == (u.scheme, u.hostname, u.port)


def _same_site(origin: str | None, url: str | None) -> bool:
    """Same eTLD+1 but not same-origin — a subdomain hop (form.x → api.x)."""
    if not origin or not url:
        return False
    try:
        o, u = urlparse(origin), urlparse(url)
    except ValueError:
        return False
    return (
        _registrable(o.hostname) != "" and _registrable(o.hostname) == _registrable(u.hostname)
    )


def random_fingerprint(
    origin: str | None = None,
    referer: str | None = None,
    *,
    url: str | None = None,
):
    """Build a browser-shaped header set.

    Pass ``url`` when the request goes to the same site the page is served
    from: a real browser then sends ``sec-fetch-site: same-origin``, and
    sending ``cross-site`` alongside the site's own Origin is a contradiction
    some backends reject outright (ZTH Academy answered ORIGIN_NOT_ALLOWED to
    every Chrome-shaped request for exactly this reason). Without ``url`` the
    value stays ``cross-site``, which is correct for the GAS/Forms adapters
    that post to a different host than their page.
    """
    browser = random.choices(
        ["chrome_win", "chrome_mac", "chrome_linux", "chrome_android",
         "firefox_win", "firefox_mac", "firefox_linux",
         "safari_mac", "safari_ios", "edge_win"],
        weights=[20, 15, 5, 8, 10, 5, 3, 12, 8, 14],
    )[0]

    if browser == "chrome_win":
        major = random.choice(CHROME_MAJOR)
        full = _chromium_full(major)
        ua = (
            f"Mozilla/5.0 ({random.choice(WIN_NT)}; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Safari/537.36"
        )
        h = _chrome_headers(ua, major)
        h["sec-ch-ua-platform"] = '"Windows"'
    elif browser == "chrome_mac":
        major = random.choice(CHROME_MAJOR)
        full = _chromium_full(major)
        ua = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X {random.choice(MAC_VER)}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Safari/537.36"
        )
        h = _chrome_headers(ua, major)
        h["sec-ch-ua-platform"] = '"macOS"'
    elif browser == "chrome_linux":
        major = random.choice(CHROME_MAJOR)
        full = _chromium_full(major)
        ua = (
            f"Mozilla/5.0 ({random.choice(LINUX_DIST)}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Safari/537.36"
        )
        h = _chrome_headers(ua, major)
        h["sec-ch-ua-platform"] = '"Linux"'
    elif browser == "chrome_android":
        major = random.choice(CHROME_MAJOR)
        full = _chromium_full(major)
        ua = (
            f"Mozilla/5.0 (Linux; Android {random.choice(ANDROID_VER)}; SM-G991B) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Mobile Safari/537.36"
        )
        h = _chrome_headers(ua, major)
        h["sec-ch-ua-mobile"] = "?1"
        h["sec-ch-ua-platform"] = '"Android"'
    elif browser == "firefox_win":
        ff = random.choice(FIREFOX_MAJOR)
        ua = (
            f"Mozilla/5.0 ({random.choice(WIN_NT)}; Win64; x64; rv:{ff}.0) "
            f"Gecko/20100101 Firefox/{ff}.0"
        )
        h = _firefox_headers(ua)
    elif browser == "firefox_mac":
        ff = random.choice(FIREFOX_MAJOR)
        ua = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X {random.choice(MAC_VER)}; rv:{ff}.0) "
            f"Gecko/20100101 Firefox/{ff}.0"
        )
        h = _firefox_headers(ua)
    elif browser == "firefox_linux":
        ff = random.choice(FIREFOX_MAJOR)
        ua = (
            f"Mozilla/5.0 ({random.choice(LINUX_DIST)}; rv:{ff}.0) "
            f"Gecko/20100101 Firefox/{ff}.0"
        )
        h = _firefox_headers(ua)
    elif browser == "safari_mac":
        sf = random.choice(SAFARI_MAJOR)
        ua = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X {random.choice(MAC_VER)}) "
            f"AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{sf} Safari/605.1.15"
        )
        h = _safari_headers(ua)
    elif browser == "safari_ios":
        ios, sf = random.choice(IOS_PROFILES)
        ua = (
            f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios} like Mac OS X) "
            f"AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{sf} "
            f"Mobile/15E148 Safari/604.1"
        )
        h = _safari_headers(ua)
    elif browser == "edge_win":
        major = random.choice(EDGE_MAJOR)
        full = _chromium_full(major)
        ua = (
            f"Mozilla/5.0 ({random.choice(WIN_NT)}; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} "
            f"Safari/537.36 Edg/{full}"
        )
        h = _edge_headers(ua, major)
    else:
        raise RuntimeError(f"unknown browser profile: {browser}")

    if origin:
        h["Origin"] = origin
    if referer:
        h["Referer"] = referer
    if "sec-fetch-site" in h and url:
        # Exact origin → same-origin; a subdomain hop on the same site
        # (form.example → api.example) → same-site. Both contradict the
        # site's own Origin header less than a blanket cross-site does.
        if _same_origin(origin, url):
            h["sec-fetch-site"] = "same-origin"
        elif _same_site(origin, url):
            h["sec-fetch-site"] = "same-site"

    headers = {k: v for k, v in h.items() if v is not None}
    return headers, ua


def is_valid_wallet(wallet: str) -> bool:
    if not wallet.startswith("0x") and not wallet.startswith("0X"):
        return False
    body = wallet[2:]
    return len(body) == 40 and all(c in "0123456789abcdefABCDEF" for c in body)

def load_proxies(path: str) -> list[str]:
    """Parse host:port:user:pass — password/user may contain ':' (maxsplit=3)."""
    proxies = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            parts = line.split(":", 3)
            if len(parts) != 4:
                skipped += 1
                log(f"skip {path}:{line_no}: expected host:port:user:pass")
                continue
            host, port, user, pwd = parts
            if not host or not port or not user:
                skipped += 1
                log(f"skip {path}:{line_no}: empty host/port/user")
                continue
            try:
                int(port)
            except ValueError:
                skipped += 1
                log(f"skip {path}:{line_no}: invalid port {port!r}")
                continue
            user_q = quote(user, safe="")
            pwd_q = quote(pwd, safe="")
            proxies.append(f"http://{user_q}:{pwd_q}@{host}:{port}")
    if skipped:
        log(f"Skipped {skipped} invalid line(s) in {path}")
    return proxies

def proxy_host_port(proxy_url: str) -> tuple[str, int]:
    parsed = urlparse(proxy_url)
    host = parsed.hostname or ""
    port = parsed.port or 80
    return host, port

# Apps Script prints its own failures as one line in a monospace <div> on the
# user-content sandbox page. The prose is localized by the exit IP — "Fungsi
# skrip tidak ditemukan: doPost", "Fonction de script introuvable : doPost",
# "Không tìm thấy hàm tập lệnh: doPost" — so the line is worth showing but not
# worth matching on.
_GAS_ERROR_LINE_RE = re.compile(
    r"font-family\s*:\s*monospace[^>]*>\s*([^<]{3,240}?)\s*<", re.I
)

# …the entry point named at the end of that line, however, is never translated,
# and "no doPost" is the one GAS failure no retry can fix: the deployment is
# serving a script version that does not define it.
_GAS_MISSING_ENTRYPOINT_RE = re.compile(r"[:：]\s*(doPost|doGet)\s*$")


def parse_gas_error_line(html_text: str) -> str:
    """Return the single line Apps Script prints on its own HTML error page."""
    m = _GAS_ERROR_LINE_RE.search(html_text or "")
    return html_lib.unescape(m.group(1)).strip()[:200] if m else ""


def gas_missing_entrypoint(html_text: str) -> str:
    """Name the entry point ('doPost'/'doGet') the deployment does not define.

    Empty string when the page is not that error. Matches in any locale: only
    the function name after the colon is compared, never the prose before it.
    """
    m = _GAS_MISSING_ENTRYPOINT_RE.search(parse_gas_error_line(html_text))
    return m.group(1) if m else ""


def parse_gas_html_error(html_text: str) -> str:
    """Extract Apps Script exception text from HTML error page (any locale)."""
    text = html_text or ""
    # e.g. Exception: Lock timeout... / Exception: Kilitleme zaman aşımı...
    m = re.search(r"Exception:\s*([^<]{5,240})", text, re.I)
    if m:
        msg = html_lib.unescape(m.group(1)).strip()
        # Drop trailing " (line N, file "Code")" noise if present
        msg = re.sub(
            r"\s*\((?:line|satır|строка|dòng|ligne|บรรทัด)[^)]*\)\s*$",
            "",
            msg,
            flags=re.I,
        )
        return msg[:200]
    if is_gas_lock_message(text):
        return "Spreadsheet lock busy (localized GAS error)"
    # Not every GAS failure is an "Exception:" — a thrown script, a missing
    # entry point and a revoked deployment all land here. Show the line the
    # page actually carries rather than the shrug that used to be returned.
    return parse_gas_error_line(text) or "GAS HTML error page"


def _has_word(text: str, word: str) -> bool:
    """
    Whole-word match (case-insensitive).

    Avoids substring traps: bare ``"lock" in "block"`` is True, but the word
    ``lock`` is not present in ``block`` / ``blocklist`` / ``blocked``.
    """
    if not text or not word:
        return False
    return re.search(rf"\b{re.escape(word)}\b", text, flags=re.I) is not None


def is_gas_lock_message(detail: str = "") -> bool:
    """
    Detect Google Sheet / LockService contention in any language.
    Messages vary by proxy IP locale (EN, TH, TR, RU, …).
    """
    d = detail or ""
    low = d.lower()
    # Latin / common substrings (phrases — no bare "lock")
    if any(
        x in low
        for x in (
            "lock timeout",
            "lock service",
            "lockservice",  # Apps Script class name (one word)
            "spreadsheet lock",
            "holding the lock",
            "another process was holding",
            "too many simultaneous",
            "service invoked too many times",
            "zaman aşımı",  # TR timeout
            "kilitleme",  # TR lock
            "таймаут",
            "блокиров",
            # Every wording below was measured against one script — MiNI
            # BRoKER's — on 2026-08-20: its sheet answered a lock in twelve
            # languages inside two hours, because Apps Script localizes the
            # exception by the exit IP. None of them carries "lock" or
            # "timeout", so before this list they were graded as verdicts and
            # the row was thrown away for a reason the next attempt would have
            # passed.
            "verrouillage",  # FR
            "verrou ",
            "bloqueo",  # ES
            "bloqueio",  # PT
            "blocco",  # IT
            "vergrendeling",  # NL
            "exklusivbearbeitung",  # DE — Google's word for the lock itself
            "zeitüberschreitung",  # DE timeout
            "chờ khóa",  # VI
            "giữ khóa",
            "блакав",  # BE
            "таймаўт",
            "блокув",  # UK
        )
    ):
        return True
    # Whole word "lock" near timeout/busy/hold/wait (not "block" / "blocklist")
    if _has_word(low, "lock") and any(
        x in low for x in ("timeout", "busy", "hold", "wait", "time")
    ):
        return True
    # Thai LockService (common via SG proxies)
    if any(x in d for x in ("หมดเวลา", "ล็อก", "การล็อก")):
        return True
    # Other CJK / localized fragments seen in the wild
    # ZH: "锁超时：另一个进程占用锁时间过久。" — the bare 锁 carries the meaning,
    # and 锁定 alone missed it (measured 2026-08-20 on a CN exit).
    if "ロック" in d or "锁" in d or "鎖" in d or "잠금" in d or "लॉक" in d:
        return True
    return False


def is_retryable_failure(detail: str = "", reason: str = "", is_html: bool = False) -> bool:
    """
    Retry anything that is not a clear business rejection (duplicate).
    GAS LockService errors are often localized — do not rely on English alone.

    Do NOT match bare substring ``lock`` (false positive on ``block`` / ``blocked``).
    Real sheet locks go through ``is_gas_lock_message``.
    """
    r = (reason or "").lower()
    d = (detail or "").lower()
    if r == "duplicate" or "duplicate" in d:
        return False
    if is_html:
        return True
    if r in (
        "gas_lock",
        "gas_error",
        "gas_busy",
        "html_or_non_json",
        "html_error",
    ):
        return True
    if is_gas_lock_message(detail):
        return True
    # Transient network / HTTP / GAS noise — no bare "lock" (see H1 audit)
    return any(
        x in d
        for x in (
            "html_error",
            "html_or_non_json",
            "non_json",
            "gas html",
            "spreadsheet",
            "timeout",
            "too many times",
            "try again",
            "zaman aşımı",
            "таймаут",
            "rate limit",
            "429",
            "502",
            "503",
            "504",
        )
    )


def format_request_error(exc: BaseException, proxy_url: str) -> str:
    """Describe a failed request, and remember that this exit caused it.

    Recording state from a formatting function is not where it belongs, and it
    is here anyway: this is the one call every one of the twenty-one adapters
    already makes at exactly this moment. The alternative is a second call
    added to twenty-one retry loops, which is twenty-one chances to forget one
    — and the exit that goes uncounted is the one that keeps taking rows.
    """
    host, port = proxy_host_port(proxy_url)
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, (ProxyError, ConnectTimeout, ReqConnectionError)) or (
        "proxyerror" in low or "sslerror" in low
    ):
        note_transport_failure(proxy_url)
    if "getaddrinfo failed" in low or "nameresolutionerror" in low or "failed to resolve" in low:
        return f"DNS/proxy resolve failed for {host}:{port}"
    if "proxyerror" in low or isinstance(exc, ProxyError):
        if "10061" in msg or "refused" in low:
            return f"proxy refused connection {host}:{port}"
        if "10060" in msg or "timed out" in low:
            return f"proxy connect timeout {host}:{port}"
        return f"proxy error via {host}:{port}: {type(exc).__name__}"
    if isinstance(exc, ConnectTimeout):
        return f"connect timeout via {host}:{port}"
    if isinstance(exc, ReqConnectionError):
        return f"connection error via {host}:{port}: {type(exc).__name__}"
    one_line = msg.replace("\n", " ")
    if len(one_line) > 180:
        one_line = one_line[:180] + "..."
    return one_line


# Details produced by ``format_request_error`` for a ProxyError. Used by the
# queue to tell "the exit died" from any other FAIL (site refuse, no_proxies…).
_PROXY_FAIL_PREFIXES = (
    "proxy error via ",
    "proxy refused connection ",
    "proxy connect timeout ",
)


def is_proxy_fail_detail(detail: str | None) -> bool:
    """True when ``detail`` is a proxy-transport FAIL from ``format_request_error``."""
    text = (detail or "").strip().lower()
    return any(text.startswith(prefix) for prefix in _PROXY_FAIL_PREFIXES)


def is_proxy_block_page_detail(detail: str | None) -> bool:
    """Reseller/HTML block page, even when it still carries the site's HTTP 404.

    Adapters that rewrite the body say ``proxy block page (ppConfig)``. Raw
    GAS/HTML answers keep ``window['ppConfig']`` in the snippet — both are
    a dead exit, not a closed form.
    """
    text = (detail or "").strip().lower()
    if not text:
        return False
    return "proxy block page" in text or "ppconfig" in text


# Refuse longer hex tails (tx hashes) and require a non-hex left boundary so
# embedded substrings inside longer hex blobs are not accepted.
WALLET_RE = re.compile(
    r"(?<![0-9a-fA-F])0x[a-fA-F0-9]{40}(?![0-9a-fA-F])",
    re.IGNORECASE,
)


def extract_wallet(text: str) -> str | None:
    """Extract first EVM address from free text; None if missing/ambiguous."""
    m = WALLET_RE.search(text or "")
    if not m:
        return None
    w = m.group(0)
    # Prefer exact-token style: if whole string is only the wallet (trimmed), OK
    return w


def normalize_twitter(handle: str, *, max_len: int = 30) -> str:
    """Bare handle without @. max_len 15 for X-compat sites, 30 for others."""
    h = (handle or "").strip().lstrip("@")
    if "twitter.com/" in h.lower() or "x.com/" in h.lower():
        return ""
    if not re.fullmatch(rf"[A-Za-z0-9_]{{1,{max_len}}}", h):
        return ""
    return h


# Exits that keep failing at the transport layer, and for how long to stop
# handing them out.
#
# `bot/exit_budget.py` already retires an exit that answers 429, but that is a
# *route* refusing us and it is opt-in per project. This is the other failure:
# the exit itself is broken — the proxy refuses, the TLS handshake dies — and it
# affects every project equally. Measured over five days, one gateway produced
# 339 ProxyError and 236 SSLError, 44% of every network error the bot logged,
# and stayed in rotation the whole time because nothing was counting.
#
# Counted in a window rather than consecutively, because there is no success
# hook to reset a counter on: over enough hours every exit collects three
# failures, and a plain tally would eventually bench the entire pool.
_PROXY_FAIL_WINDOW_S = 600.0
_PROXY_FAIL_MAX = 3
_PROXY_BENCH_S = 900.0
# Escalation: the Nth bench of the same exit lasts 2**(N-1) times the base
# TTL, capped — a first offence is 15 minutes, a chronic offender ends up out
# for a full day. Measured over 7 days of submissions the failures are spread
# thin (405 exits, max 9 hits each), so a flat 15-minute bench let the same
# dead exits come back every quarter hour forever.
_PROXY_BENCH_MAX_S = 86400.0
# How long a strike is remembered after the last bench. Past this the exit is
# fully forgiven — recovering exits must be able to start over.
_PROXY_STRIKE_MEMORY_S = 86400.0
_PROXY_LEDGER_MAX = 20000

# The bench ledger is mirrored to STATE_DIR (hashes, never credentials — same
# rule as exit_budget) so a restart cannot put dead exits back in rotation.
# Tests set this to False to keep the production ledger untouched.
_BENCH_PERSIST = True
_BENCH_PATH_OVERRIDE: str | None = None

_proxy_health_lock = threading.Lock()
_proxy_fails: dict[str, deque[float]] = {}
# bench hash -> (benched_at_wall, strikes, strike_memory_until_wall)
_proxy_bench: dict[str, tuple[float, int, float]] = {}
_bench_loaded = False


def _bench_state_path():
    if not _BENCH_PERSIST:
        return None
    if _BENCH_PATH_OVERRIDE is not None:
        return _BENCH_PATH_OVERRIDE or None
    try:
        from bot import config

        return os.path.join(str(config.STATE_DIR), "proxy_bench.json")
    except Exception:  # noqa: BLE001 — never break a submit over state plumbing
        return None


def _bench_key(proxy_url: str) -> str:
    return hashlib.sha1(proxy_url.encode("utf-8", "replace")).hexdigest()[:16]


def _bench_ensure_loaded() -> None:
    """Read the mirror once per process. Cheap, and failures are non-fatal."""
    global _bench_loaded
    if _bench_loaded:
        return
    _bench_loaded = True
    path = _bench_state_path()
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return
        now = time.time()
        for key, entry in raw.items():
            try:
                benched_at = float(entry[0])
                strikes = max(1, int(entry[1]))
                strike_until = float(entry[2])
                if strike_until > now:
                    _proxy_bench[str(key)] = (benched_at, strikes, strike_until)
            except (TypeError, ValueError, IndexError):
                continue
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        log(f"[proxy] bench mirror unreadable, starting clean: {exc!r}")


def _bench_mirror_write_locked() -> None:
    path = _bench_state_path()
    if not path:
        return
    now = time.time()
    payload = {
        key: [round(entry[0], 3), entry[1], round(entry[2], 3)]
        for key, entry in _proxy_bench.items()
        if entry[2] > now
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".proxy_bench.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log(f"[proxy] bench mirror write failed: {exc!r}")


def note_transport_failure(proxy_url: str | None) -> None:
    """This exit failed to carry a request. Three in ten minutes benches it.

    Benching is escalating and persistent: each repeat doubles the bench TTL
    (15 min → 30 → 1 h → … capped at 24 h), and the ledger is mirrored to
    STATE_DIR so a restart does not hand the same dead exit new rows.
    """
    if not proxy_url or proxy_url == "direct":
        return
    now = time.time()
    with _proxy_health_lock:
        _bench_ensure_loaded()
        if len(_proxy_fails) > _PROXY_LEDGER_MAX:
            _proxy_fails.clear()
        hits = _proxy_fails.get(proxy_url)
        if hits is None:
            hits = _proxy_fails[proxy_url] = deque()
        cutoff = now - _PROXY_FAIL_WINDOW_S
        while hits and hits[0] <= cutoff:
            hits.popleft()
        hits.append(now)
        if len(hits) >= _PROXY_FAIL_MAX:
            key = _bench_key(proxy_url)
            benched_at, strikes, _ = _proxy_bench.get(key, (0.0, 0, 0.0))
            strikes += 1
            _proxy_fails.pop(proxy_url, None)
            _proxy_bench[key] = (
                now,
                strikes,
                now + _PROXY_STRIKE_MEMORY_S,
            )
            ttl = min(_PROXY_BENCH_S * (2 ** (strikes - 1)), _PROXY_BENCH_MAX_S)
            _bench_mirror_write_locked()
            log(
                f"[proxy] benched {error_proxy_label(proxy_url)} for {int(ttl)}s "
                f"(strike {strikes})"
            )


def proxy_is_benched(proxy_url: str, now: float | None = None) -> bool:
    moment = time.time() if now is None else now
    with _proxy_health_lock:
        _bench_ensure_loaded()
        entry = _proxy_bench.get(_bench_key(proxy_url))
        if entry is None:
            return False
        benched_at, strikes, strike_until = entry
        ttl = min(_PROXY_BENCH_S * (2 ** (strikes - 1)), _PROXY_BENCH_MAX_S)
        if moment - benched_at >= ttl or moment >= strike_until:
            if moment >= strike_until:
                _proxy_bench.pop(_bench_key(proxy_url), None)
            return False
        return True


def proxy_health_stats() -> dict:
    with _proxy_health_lock:
        _bench_ensure_loaded()
        return {
            "benched": len(_proxy_bench),
            "failing": len(_proxy_fails),
        }


def reset_proxy_health() -> None:
    """Tests, and `/reload` when the operator has swapped the pool.

    Wipes the mirror too when persistence is on: `/reload` means the pool was
    replaced, and benches keyed against the old lines are meaningless. Tests
    keep `_BENCH_PERSIST = False` so they can never touch the real ledger.
    """
    global _bench_loaded
    with _proxy_health_lock:
        _proxy_fails.clear()
        _proxy_bench.clear()
        _bench_loaded = True
        path = _bench_state_path()
        if path:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                log(f"[proxy] bench mirror removal failed: {exc!r}")


def choose_proxy_url(proxies: list[str], proxy_idx: int, attempt: int) -> str | None:
    """Return proxy URL for this attempt, or None if pool empty (no direct).

    Benched exits are stepped over rather than removed: if every one of them is
    benched — the whole gateway is down, not one line of it — the original
    choice stands. A request that might fail still beats no request, and the
    caller already treats a transport failure as retryable.
    """
    if not proxies:
        return None
    n = len(proxies)
    if n == 1:
        return proxies[0]
    idx = (proxy_idx + attempt - 1 + (attempt > 1) * random.randint(0, n - 1)) % n
    now = time.time()
    for step in range(n):
        candidate = proxies[(idx + step) % n]
        if not proxy_is_benched(candidate, now):
            return candidate
    return proxies[idx]


def proxy_dict_for(proxy_url: str | None) -> dict | None:
    if not proxy_url or proxy_url == "direct":
        return None
    return {"http": proxy_url, "https": proxy_url}


NO_PROXIES_DETAIL = "no_proxies: direct HTTP disabled (proxy required)"


def require_proxy_pool(proxies: list | None) -> tuple[list[str], str | None]:
    """
    Enforce non-empty proxy list (AUD-001).

    Returns (clean_list, error_detail). If error_detail is set, caller must
    return FAIL and must not call requests.
    """
    out = [p for p in (proxies or []) if p]
    if not out:
        return [], NO_PROXIES_DETAIL
    return out, None


def no_proxies_result(
    *,
    handle: str = "",
    wallet: str = "",
    idx: int = 0,
    extra: dict | None = None,
) -> dict:
    """Standard FAIL payload when proxies are empty."""
    r = {
        "handle": handle or "",
        "wallet": wallet or "",
        "status": "FAIL",
        "detail": NO_PROXIES_DETAIL,
        "idx": idx,
    }
    if extra:
        r.update(extra)
    return r


def is_html_body(raw: str) -> bool:
    s = (raw or "").lstrip()[:15].lower()
    return s.startswith("<!doctype") or s.startswith("<html")


# Google serves a closed form's viewform as a 302 to /closedform. Following it
# yields HTTP 200 with no fbzx, which the Forms adapters used to report as
# "form token unavailable" — indistinguishable from a proxy interstitial, so
# nothing alerted and one route burned ~1100 retries overnight (2026-08-12).
GOOGLE_FORM_CLOSED_MARKERS = (
    "no longer accepting responses",
    "form is no longer accepting",
)
GOOGLE_FORM_CLOSED_DETAIL = "form closed / no longer accepting responses"


def is_google_form_closed(response: Any) -> bool:
    """True when a Forms GET landed on the closed-form page.

    The redirected URL is checked first because it is language-proof: the body
    markers only match when the request pinned an English locale, and Forms
    localizes by Accept-Language.
    """
    url = str(getattr(response, "url", "") or "").lower()
    if "/closedform" in url:
        return True
    body = (getattr(response, "text", "") or "").lower()
    return any(marker in body for marker in GOOGLE_FORM_CLOSED_MARKERS)


# What a row waits after a Google Sheet lock when it is *not* standing aside for
# it. Long enough that two of our own attempts do not collide, short enough that
# we are asking again the moment the lock frees rather than after somebody
# else's row has taken it.
CONTEND_LOCK_WAIT = (0.6, 2.0)


def backoff_sleep(
    attempt: int,
    retry_delay: float,
    last_detail: str = "",
    *,
    contend_lock: bool = False,
) -> float:
    """Compute backoff seconds (caller sleeps).

    ``contend_lock`` picks the short wait for a Google Sheet lock. The default
    long one (12s * attempt) is patience: it hands the lock to whoever asks next,
    which on a busy sheet is somebody else's registration. A project that sets
    this instead comes straight back and takes the lock the moment it is free —
    the operator's rule since 2026-08-20: our rows do not queue behind theirs.
    """
    backoff = retry_delay * attempt + random.uniform(0.5, 2.5)
    low = (last_detail or "").lower()
    if is_gas_lock_message(last_detail):
        if "dns" not in low and "resolve" not in low:
            backoff = (
                random.uniform(*CONTEND_LOCK_WAIT)
                if contend_lock
                else 12 * attempt + random.uniform(4.0, 10.0)
            )
    if "dns" in low or "resolve" in low:
        backoff = random.uniform(0.8, 2.0)
    return backoff


def error_proxy_label(proxy_url: str | None) -> str:
    if not proxy_url or proxy_url == "direct":
        return "http://direct:0"
    return proxy_url

def is_captcha_solver_permanent_error(exc: BaseException | str) -> bool:
    """True if retrying would only burn paid solver balance (bad key, zero balance, etc.)."""
    msg = str(exc or "").lower()
    keys = (
        "zero_balance",
        "insufficient balance",
        "error_zero_balance",
        "error_key_does_not_exist",
        "wrong user key",
        "error_wrong_user_key",
        "error_key_denied",
        "invalid key",
        "error_api_key",
        "account suspended",
        "ip banned",
        "error_ip_blocked",
    )
    return any(k in msg for k in keys)
