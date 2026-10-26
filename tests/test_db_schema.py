"""Numbered schema migrations with a version stamp (bot/db/schema.py).

Three properties worth pinning:

* a fresh database is stamped to the latest version in one ``init_db()``;
* a database whose stamp was lost (restored backup, hand-edited table)
  converges instead of crashing — every migration keeps its idempotency
  guards;
* the stamp lives in ``bot_settings`` under ``db_schema_version`` and is a
  plain integer a human can read off the live database.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="db_schema_"))
_DB = _TMP / "t.db"

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ["LIMITS_CACHE_TTL"] = "0"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import db as bot_db  # noqa: E402
from bot.db.schema import SCHEMA_VERSION  # noqa: E402

bot_db.DB_PATH = str(_DB)  # type: ignore[attr-defined]


def _version() -> int:
    with bot_db.db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = 'db_schema_version'"
        ).fetchone()
    return int(row["value"]) if row else 0


def test_fresh_db_is_stamped_to_latest() -> None:
    bot_db.init_db()
    assert _version() == SCHEMA_VERSION


def test_reinit_is_a_noop() -> None:
    bot_db.init_db()
    with bot_db.db() as conn:
        before = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    bot_db.init_db()
    with bot_db.db() as conn:
        after = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    assert before == after
    assert _version() == SCHEMA_VERSION


def test_lost_stamp_converges() -> None:
    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute(
            "DELETE FROM bot_settings WHERE key = 'db_schema_version'"
        )
        conn.commit()
    assert _version() == 0
    bot_db._initialized_paths.clear()  # type: ignore[attr-defined]
    bot_db.init_db()
    assert _version() == SCHEMA_VERSION


def test_versioned_db_runs_only_new_migrations() -> None:
    """A database stamped below latest re-runs the tail, idempotently."""
    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute(
            "UPDATE bot_settings SET value = '2' WHERE key = 'db_schema_version'"
        )
        conn.commit()
    bot_db._initialized_paths.clear()  # type: ignore[attr-defined]
    bot_db.init_db()
    assert _version() == SCHEMA_VERSION
