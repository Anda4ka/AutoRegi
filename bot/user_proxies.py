"""Per-user proxy files (`/proxy`). Resolve: own → shared → none. Limits: AGENTS.md §10."""

from __future__ import annotations

import socket
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from bot import config
from bot.i18n import t

ProxySource = Literal["own", "shared", "direct", "none"]


@dataclass
class ProxyResolution:
    proxies: list[str]
    source: ProxySource


@dataclass
class ProxyParseError:
    lineno: int
    message: str


@dataclass
class ProxyParseResult:
    urls: list[str]
    errors: list[ProxyParseError]
    raw_lines: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.urls) and not self.errors


def user_proxy_path(user_id: int) -> Path:
    raw = os.getenv("USER_PROXY_DIR", "").strip()
    base = Path(raw).expanduser() if raw else config.USER_PROXY_DIR
    if not base.is_absolute():
        base = config.BASE_DIR / base
    return base / f"{int(user_id)}.txt"


def load_user_proxy_urls(user_id: int) -> list[str]:
    """Return valid proxy URLs only. Corrupt/unparseable file → empty list."""
    path = user_proxy_path(user_id)
    if not path.is_file():
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    text = data.decode("utf-8-sig", errors="replace")
    result = parse_proxy_text(text)
    if not result.ok:
        return []
    return list(result.urls)


def has_own_proxies(user_id: int) -> bool:
    """True only if at least one *valid* proxy line loads (same as resolve OWN)."""
    return bool(load_user_proxy_urls(user_id))


def own_proxy_file_exists(user_id: int) -> bool:
    """File present with non-empty non-comment lines (may be corrupt)."""
    path = user_proxy_path(user_id)
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return True
    return False


def parse_proxy_line(
    line: str,
    lineno: int = 1,
    *,
    lang: str = "ru",
) -> tuple[str | None, ProxyParseError | None]:
    """
    Parse one proxy line → requests URL or error.

    Formats:
      host:port:user:pass           → http://…
      socks5:host:port:user:pass    → socks5://…
      socks5h:host:port:user:pass   → socks5h://…
    Auth required always.
    """
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None, None

    scheme = "http"
    body = s
    low = s.lower()
    if low.startswith("socks5h:"):
        scheme = "socks5h"
        body = s.split(":", 1)[1]
    elif low.startswith("socks5:"):
        scheme = "socks5"
        body = s.split(":", 1)[1]

    parts = body.split(":", 3)
    if len(parts) != 4:
        return None, ProxyParseError(
            lineno,
            t("proxy.error.format", lang),
        )
    host, port_s, user, pwd = (p.strip() for p in parts)
    if not host or not port_s or not user or pwd is None or pwd == "":
        return None, ProxyParseError(
            lineno,
            t("proxy.error.required", lang),
        )
    try:
        port = int(port_s)
        if port < 1 or port > 65535:
            raise ValueError("port range")
    except ValueError:
        return None, ProxyParseError(
            lineno,
            t("proxy.error.port", lang, port=port_s),
        )

    user_q = quote(user, safe="")
    pwd_q = quote(pwd, safe="")
    url = f"{scheme}://{user_q}:{pwd_q}@{host}:{port}"
    return url, None


def parse_proxy_text(text: str, *, lang: str = "ru") -> ProxyParseResult:
    """Strict: any bad data line → errors list; caller rejects whole file."""
    urls: list[str] = []
    errors: list[ProxyParseError] = []
    raw_lines = 0
    for i, line in enumerate((text or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        raw_lines += 1
        if raw_lines > config.USER_PROXY_MAX_LINES:
            errors.append(
                ProxyParseError(
                    i,
                    t(
                        "proxy.error.too_many",
                        lang,
                        max=config.USER_PROXY_MAX_LINES,
                    ),
                )
            )
            break
        url, err = parse_proxy_line(line, i, lang=lang)
        if err:
            errors.append(err)
            continue
        if url:
            urls.append(url)
    if raw_lines == 0 and not errors:
        errors.append(ProxyParseError(0, t("proxy.error.no_rows", lang)))
    return ProxyParseResult(urls=urls, errors=errors, raw_lines=raw_lines)


def save_user_proxies_from_bytes(
    user_id: int,
    data: bytes,
    *,
    lang: str = "ru",
) -> tuple[bool, str, int]:
    """
    Validate and atomically replace user proxy file.

    Returns (ok, message_html_or_plain, count).
    On failure previous file is untouched.
    """
    if not data:
        return False, t("proxy.error.empty_file", lang), 0
    if len(data) > config.USER_PROXY_MAX_BYTES:
        kb = config.USER_PROXY_MAX_BYTES // 1024
        return False, t("proxy.error.too_big", lang, kb=kb), 0

    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return False, t("proxy.error.encoding", lang), 0

    parsed = parse_proxy_text(text, lang=lang)
    if not parsed.ok:
        lines = [t("proxy.error.rejected", lang), ""]
        for e in parsed.errors[:20]:
            if e.lineno:
                lines.append(f"• стр. {e.lineno}: {e.message}")
            else:
                lines.append(f"• {e.message}")
        if len(parsed.errors) > 20:
            lines.append(t("proxy.error.more", lang, n=len(parsed.errors) - 20))
        return False, "\n".join(lines), 0

    path = user_proxy_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write normalized host:port:user:pass lines (keep socks prefix if any)
    out_lines: list[str] = []
    for url in parsed.urls:
        out_lines.append(_url_to_storage_line(url))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    _invalidate_limits(user_id)
    return True, t("proxy.saved", lang, n=len(parsed.urls)), len(parsed.urls)


def _invalidate_limits(user_id: int) -> None:
    """Own proxies are part of the tier — drop the cached Caps after a write."""
    try:
        from bot import limits as bot_limits

        bot_limits.invalidate(int(user_id))
    except Exception:  # pragma: no cover - cache drop must not break upload
        pass


def _url_to_storage_line(url: str) -> str:
    p = urlparse(url)
    scheme = (p.scheme or "http").lower()
    host = p.hostname or ""
    port = p.port or ""
    user = p.username or ""
    pwd = p.password or ""
    # urlparse may unquote; re-store raw from netloc is hard — use decoded
    from urllib.parse import unquote

    user = unquote(user)
    pwd = unquote(pwd)
    if scheme in ("socks5", "socks5h"):
        return f"{scheme}:{host}:{port}:{user}:{pwd}"
    return f"{host}:{port}:{user}:{pwd}"


def clear_user_proxies(user_id: int) -> bool:
    path = user_proxy_path(user_id)
    if path.is_file():
        path.unlink()
        _invalidate_limits(user_id)
        return True
    return False


def mask_proxy_url(url: str) -> str:
    """host:port only — never credentials."""
    try:
        p = urlparse(url)
        host = p.hostname or "?"
        port = p.port or "?"
        scheme = p.scheme or "http"
        return f"{scheme}://{host}:{port}"
    except Exception:
        return "(proxy)"


def dns_sample_check(proxies: list[str], sample: int = 3) -> tuple[int, int]:
    """Return (ok_count, tried) for first `sample` unique hosts."""
    seen: set[str] = set()
    ok = 0
    tried = 0
    for url in proxies:
        try:
            p = urlparse(url)
            host = p.hostname or ""
            if not host or host in seen:
                continue
            seen.add(host)
            tried += 1
            socket.getaddrinfo(host, p.port or 80, type=socket.SOCK_STREAM)
            ok += 1
        except Exception:
            pass
        if tried >= sample:
            break
    return ok, tried


def resolve_proxies(
    user_id: int,
    *,
    allow_direct: bool,
    shared: list[str],
    is_vip_user: bool | None = None,
    prefer_direct: bool = False,
) -> ProxyResolution:
    """
    own (valid only) → shared → none

    Operator policy: **all projects use proxies**. ``prefer_direct`` and
    VIP ``allow_direct`` direct-fallback are ignored (kept as kwargs for
    registry/call-site compatibility). Free users never used direct anyway.

    If a non-empty own file exists but does not parse, return none (do NOT fall
    back to shared while user is billed as own-tier).
    """
    uid = int(user_id)
    # prefer_direct / allow_direct intentionally unused — no direct path.
    _ = (allow_direct, prefer_direct, is_vip_user)

    urls = load_user_proxy_urls(uid)
    if urls:
        return ProxyResolution(proxies=urls, source="own")
    if own_proxy_file_exists(uid):
        # Corrupt own file: block rather than burn shared pool
        return ProxyResolution(proxies=[], source="none")
    if shared:
        return ProxyResolution(proxies=list(shared), source="shared")
    return ProxyResolution(proxies=[], source="none")
