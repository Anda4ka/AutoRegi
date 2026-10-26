"""SQLite access layer: users, submissions, stats, payments, outbox, settings.

Split (2026-09-09) out of a single 2.5k-line ``bot/db.py`` into a package —
one module per domain, schema and migrations in ``bot.db.schema``. This file
keeps the connection plumbing and the shared helpers, then re-exports every
public name so ``from bot import db as bot_db`` / ``from bot.db import X``
call sites (and tests that assign ``bot_db.DB_PATH``) do not change:

* ``db()`` and ``init_db()`` read the module-global ``DB_PATH`` at call time —
  that is what tests patch, so they must live here, not in a submodule.
* Domain modules import ``db`` / ``_now`` / helpers from here; this file
  defines them **before** importing the submodules, so the cycle resolves.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from bot.config import (  # noqa: F401
    BOT_RESULTS,
    DB_PATH,
    FREE_OK_LIMIT,
)

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()
_log = logging.getLogger(__name__)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """One connection per call, committed on success and **closed** either way.

    This used to return the connection bare, and every one of the call sites
    below spells it ``with db() as conn``. That reads like a resource being
    released; it is not. ``sqlite3.Connection.__exit__`` ends the *transaction*
    and leaves the connection — and its file descriptors — open. What actually
    reclaimed them was the cyclic garbage collector, because the connection
    ends up in a reference cycle rather than at refcount zero.

    Measured on the live database: fifty of these blocks leaked 57 descriptors,
    which a manual ``gc.collect()`` then returned in full. In the running bot
    that came to roughly ten a minute against a soft ``RLIMIT_NOFILE`` of 1024,
    and it took the process down twice on 2026-08-17 — once as ``[Errno 24]
    Too many open files`` while writing a queue payload, once as sqlite's
    ``unable to open database file``, which is the same exhaustion wearing a
    misleading message: the database was present and readable, there was simply
    no descriptor left to open it with.

    The generator keeps the old transaction semantics exactly — ``with conn``
    still commits on a clean exit and rolls back on an exception — and adds the
    ``close()`` that was missing. Call sites do not change.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            pass
        with conn:
            yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    key = str(Path(DB_PATH).resolve())
    with _init_lock:
        if key in _initialized_paths and Path(DB_PATH).exists():
            return
        with db() as conn:
            from bot.db.schema import apply_schema

            apply_schema(conn)
        _initialized_paths.add(key)


def parse_iso(value: str | None) -> datetime | None:
    """Parse a stored ISO timestamp; naive values are read as UTC."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _invalidate_limits(user_id: int | None = None) -> None:
    """Drop the tier cache after anything that can change a user's limit.

    Imported lazily: bot.limits imports this module, so a module-level import
    here would be circular.
    """
    try:
        from bot import limits as bot_limits

        bot_limits.invalidate(user_id)
    except Exception:  # pragma: no cover - cache drop must never break a write
        pass


def _fetchone_healing(sql: str, args: tuple) -> sqlite3.Row | None:
    """Run a read that may hit a not-yet-created table (same idiom as get_setting)."""
    try:
        with db() as conn:
            return conn.execute(sql, args).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower() and "no such column" not in str(exc).lower():
            raise
        init_db()
        with db() as conn:
            return conn.execute(sql, args).fetchone()


# Domain modules — imported last: they bind db/_now/helpers from this module.
from bot.db import (  # noqa: F401
    audit,
    outbox,
    payments,
    settings,
    stats,
    suggestions,
    users,
)
from bot.db.audit import *
from bot.db.outbox import *
from bot.db.payments import *
from bot.db.settings import *
from bot.db.stats import *

# Private names the rest of the code (and tests) still read off this module.
from bot.db.stats import _PROJECT_SETTLED_MAX, _is_evm_wallet  # noqa: F401
from bot.db.suggestions import *
from bot.db.users import *
