"""Per-user captcha solver keys (capsolver / 2captcha / anti-captcha).

A captcha project is normally VIP-only because every row spends a solve out
of the operator's own balance. A user who brings a valid key of their own
pays for their own rows, so the same project opens for them at their normal
tier (operator rule, 2026-09-08). The policy is **own key only**: when the
key dies mid-run the row fails with a clear reason — the operator's balance
is never a fallback for somebody else's traffic.

Storage mirrors `/data` and `/proxy`: one JSON file per user under the
persistent state dir, chmod 0600, never logged, never echoed back whole
(the UI shows a masked tail only).
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from bot import config

_dir_lock = threading.Lock()

PROVIDER_NAMES = ("capsolver", "2captcha", "anticaptcha")
# Operator rule 2026-09-14: a user may keep one key per provider, up to three
# total; the solver walks them as a chain (first live key wins, an empty
# account is stepped over like the operator's own providers).
MAX_KEYS = 3

_KEY_RE = re.compile(r"^[A-Za-z0-9._\-]{8,128}$")


def user_captcha_dir() -> Path:
    path = Path(config.STATE_DIR) / "user_captcha"
    with _dir_lock:
        path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def user_captcha_path(user_id: int) -> Path:
    return user_captcha_dir() / f"{int(user_id)}.json"


def normalize_provider(value: str) -> str:
    raw = str(value or "").strip().lower()
    for suffix in (".com", ".io"):
        raw = raw.removesuffix(suffix)
    raw = raw.replace("-", "").replace("_", "").replace(".", "")
    if raw in ("2captcha", "twocaptcha"):
        return "2captcha"
    if raw in ("anticaptcha", "anti_captcha"):
        return "anticaptcha"
    if raw == "capsolver":
        return "capsolver"
    return ""


def _valid_secret(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def _read_raw(user_id: int) -> dict:
    path = user_captcha_path(user_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_keys(user_id: int) -> list[tuple[str, str]]:
    """``[(provider, key), …]`` in stored order; empty when the user has none.

    A corrupt file must never read as "has a key" (16.10's own-file rule), so
    a bad shape is dropped row by row, not guessed around.
    """
    data = _read_raw(user_id)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    rows = data.get("keys")
    if isinstance(rows, list):
        candidates = rows
    elif data.get("provider") or data.get("key"):
        # Legacy single-key file → a one-element chain.
        candidates = [{"provider": data.get("provider"), "key": data.get("key")}]
    else:
        candidates = []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        provider = normalize_provider(str(row.get("provider") or ""))
        key = str(row.get("key") or "").strip()
        if not provider or not _valid_secret(key) or provider in seen:
            continue
        seen.add(provider)
        out.append((provider, key))
    return out[:MAX_KEYS]


def get_key(user_id: int) -> tuple[str, str] | None:
    """First stored key, or None — compat for single-key callers."""
    got = get_keys(user_id)
    return got[0] if got else None


def has_key(user_id: int) -> bool:
    return bool(get_keys(user_id))


def save_key(user_id: int, provider: str, key: str) -> tuple[bool, str]:
    """Upsert this provider's key, keeping the others (one per provider, ≤3).

    Shape check only. Validation against the live API is separate
    (``validate_key``): the bot must be able to save an offline-shaped key
    without paying a network round-trip per keystroke.
    """
    prov = normalize_provider(provider)
    if not prov:
        return False, "unknown provider"
    key = str(key or "").strip()
    if not _valid_secret(key):
        return False, "bad key format"
    rows = get_keys(user_id)
    if prov in (p for p, _k in rows):
        # Replace in place so a re-saved key keeps its place in the chain.
        rows = [(p, key if p == prov else k) for p, k in rows]
    else:
        rows = rows + [(prov, key)]
    if len(rows) > MAX_KEYS:
        return False, "too many keys"
    path = user_captcha_path(user_id)
    path.write_text(
        json.dumps(
            {"keys": [{"provider": p, "key": k} for p, k in rows]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return True, ""


def remove_key(user_id: int, provider: str) -> bool:
    """Drop one provider's key; True when something was removed."""
    prov = normalize_provider(provider)
    if not prov:
        return False
    rows = [(p, k) for p, k in get_keys(user_id) if p != prov]
    had = len(get_keys(user_id)) != len(rows)
    if not rows:
        clear_key(user_id)
        return had
    path = user_captcha_path(user_id)
    path.write_text(
        json.dumps(
            {"keys": [{"provider": p, "key": k} for p, k in rows]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return had


def clear_key(user_id: int) -> None:
    user_captcha_path(user_id).unlink(missing_ok=True)


def masked(user_id: int) -> str:
    """Safe to show back: one ``provider: …tail`` line per stored key."""
    lines = [f"{provider}: …{key[-4:]}" for provider, key in get_keys(user_id)]
    return "\n".join(lines)


def validate_key(provider: str, key: str) -> tuple[bool, float | None, str]:
    """Ask the provider's own getBalance whether this key is real.

    Returns ``(ok, balance_or_None, detail)``. Network errors are ``ok=False``
    with a retryable detail — the caller decides the wording.
    """
    import requests

    from bot.captcha import PROVIDERS, _error_detail
    prov = normalize_provider(provider)
    if not prov:
        return False, None, "unknown provider"
    secret = str(key or "").strip()
    if not _valid_secret(secret):
        return False, None, "bad key format"
    meta = next((p for p in PROVIDERS if p.name == prov), None)
    if meta is None:
        return False, None, "unknown provider"
    try:
        data = requests.post(
            f"{meta.base_url}/getBalance",
            json={"clientKey": secret},
            timeout=30,
        ).json()
    except Exception as exc:  # noqa: BLE001 — any transport shape means "ask again later"
        return False, None, f"{prov} unreachable: {exc}"[:300]
    if data.get("errorId"):
        return False, None, _error_detail(meta, data)
    try:
        return True, float(data.get("balance") or 0.0), ""
    except (TypeError, ValueError):
        return False, None, f"{prov}: unreadable balance"
