"""The shared proxy pool: load, merge, cache.

Moved out of tgbot.py so UI and submit paths can read the pool without
importing the whole Telegram shell. The module-level state (_proxies,
_proxies_loaded) is THE pool; tests install a fixture pool by setting
`proxy_pool._proxies_loaded = True` and mutating `proxy_pool._proxies[:]`.

The pool can be taken offline without deleting the files: env
``SHARED_PROXIES=0`` is a hard off; SQLite ``shared_proxies_off=1`` is the
admin toggle (same shape as new-project alerts). While off, ``get_proxies``
returns an empty list so resolve falls through to own-or-none.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from bot import config
from bot.http_client import load_proxies as load_shared_proxies

_proxies: list[str] = []
_proxies_loaded = False
_proxies_lock = threading.Lock()
_enabled_cache: bool | None = None

SETTING_OFF = "shared_proxies_off"


def _proxy_paths() -> list[Path]:
    """Ordered shared proxy files: PROXY_FILE then EXTRA_PROXY_FILES."""
    paths: list[Path] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        p = Path(raw)
        if not p.is_absolute():
            p = config.BASE_DIR / p
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            return
        seen.add(key)
        paths.append(p)

    if config.PROXY_FILE:
        _add(config.PROXY_FILE)
    for extra in getattr(config, "EXTRA_PROXY_FILES", []) or []:
        _add(extra)
    return paths

def load_bot_proxies() -> list[str]:
    """Load + merge shared proxy files; dedupe URLs (first file wins)."""
    merged: list[str] = []
    seen: set[str] = set()
    per_file: list[str] = []
    for path in _proxy_paths():
        if not path.is_file():
            print(f"[proxies] missing {path}", flush=True)
            continue
        chunk = list(load_shared_proxies(str(path)) or [])
        added = 0
        for url in chunk:
            if url in seen:
                continue
            seen.add(url)
            merged.append(url)
            added += 1
        per_file.append(f"{path.name}:{added}/{len(chunk)}")
    if per_file:
        print(f"[proxies] merged {' + '.join(per_file)} → unique={len(merged)}", flush=True)
    return merged

def _env_hard_off() -> bool:
    return os.environ.get("SHARED_PROXIES", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


def shared_enabled() -> bool:
    """Whether submit paths may use the shared pool.

    Env ``SHARED_PROXIES=0`` wins (cannot be turned back on from the admin
    panel). Otherwise SQLite ``shared_proxies_off=1`` is the live toggle.
    """
    global _enabled_cache
    if _env_hard_off():
        return False
    if _enabled_cache is not None:
        return _enabled_cache
    from bot import db as bot_db

    try:
        off = bot_db.get_setting(SETTING_OFF, "") == "1"
    except Exception:
        # Tests / first boot before init_db: fail open so a fixture pool still
        # works. An explicit env off already returned above.
        _enabled_cache = True
        return True
    _enabled_cache = not off
    return _enabled_cache


def set_shared_enabled(on: bool, *, admin_id: int | None = None) -> None:
    """Admin toggle. A hard env off still wins over this write."""
    global _enabled_cache
    from bot import db as bot_db

    bot_db.set_setting(SETTING_OFF, "0" if on else "1", updated_by=admin_id)
    _enabled_cache = False if _env_hard_off() else bool(on)
    print(
        f"[proxies] shared pool {'ON' if _enabled_cache else 'OFF'}"
        + (" (env SHARED_PROXIES=0)" if _env_hard_off() else ""),
        flush=True,
    )


def get_proxies() -> list[str]:
    """Proxies the submit path may actually use. Empty while the pool is off."""
    if not shared_enabled():
        return []
    global _proxies_loaded
    with _proxies_lock:
        if not _proxies_loaded:
            _proxies[:] = load_bot_proxies()
            _proxies_loaded = True
        return list(_proxies)


def loaded_count() -> int:
    """How many URLs are on disk, even if the pool is currently off."""
    global _proxies_loaded
    with _proxies_lock:
        if not _proxies_loaded:
            _proxies[:] = load_bot_proxies()
            _proxies_loaded = True
        return len(_proxies)


def reload_shared() -> int:
    """Re-read the shared files now. Returns the loaded (not served) size."""
    global _proxies_loaded
    with _proxies_lock:
        _proxies[:] = load_bot_proxies()
        _proxies_loaded = True
        return len(_proxies)
