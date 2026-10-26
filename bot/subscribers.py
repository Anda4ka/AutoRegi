"""Active subscribers list → raised OK limit on shared proxies.

File: one Telegram user_id per line (comments # and blank lines ignored).
Reload: bot start + admin /reload (proxies + subscribers + projects).
"""

from __future__ import annotations

import threading
from pathlib import Path

from bot import config

_lock = threading.Lock()
_ids: set[int] = set()
_loaded_path: str = ""
_loaded_count: int = 0


def subscribers_path() -> Path:
    raw = (getattr(config, "SUBSCRIBERS_FILE", None) or "").strip()
    if not raw:
        return config.BASE_DIR / "active_subscribers.txt"
    p = Path(raw)
    if not p.is_absolute():
        p = config.BASE_DIR / p
    return p


def load_subscribers(path: str | Path | None = None) -> int:
    """
    Reload id set from file. Missing file → empty set (not an error).
    Returns number of unique ids loaded.
    """
    global _loaded_path, _loaded_count
    p = Path(path) if path is not None else subscribers_path()
    found: set[int] = set()
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            print(f"[subscribers] read failed {p}: {e}", flush=True)
            text = ""
        for i, line in enumerate(text.splitlines(), start=1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            # allow "123456  # note"
            token = s.split()[0].split("#")[0].strip()
            if token.isdigit() or (token.startswith("-") and token[1:].isdigit()):
                found.add(int(token))
            else:
                print(f"[subscribers] skip {p.name}:{i}: not an id ({token[:32]!r})", flush=True)
    with _lock:
        _ids.clear()
        _ids.update(found)
        _loaded_path = str(p)
        _loaded_count = len(found)
    try:
        # The file feeds a tier, so a reload must drop every cached Caps.
        from bot import limits as bot_limits

        bot_limits.invalidate()
    except Exception:  # pragma: no cover - cache drop must not break reload
        pass
    print(
        f"[subscribers] loaded {_loaded_count} id(s) from {_loaded_path}"
        + ("" if p.is_file() else " (file missing)"),
        flush=True,
    )
    return _loaded_count


def is_subscriber(user_id: int) -> bool:
    uid = int(user_id)
    with _lock:
        return uid in _ids


def subscriber_count() -> int:
    with _lock:
        return len(_ids)


def subscriber_ids_snapshot() -> set[int]:
    with _lock:
        return set(_ids)
