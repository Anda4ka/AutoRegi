from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


_ENV_KEYS = (
    "AUTOREG_STATE_DIR",
    "AUTOREG_MIGRATE_LEGACY",
    "BOT_DB",
    "BOT_RESULTS",
    "PROXY_FILE",
    "SUBSCRIBERS_FILE",
    "USER_PROXY_DIR",
    "USER_DATA_DIR",
    "JOB_PAYLOAD_DIR",
)


class PersistentStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["AUTOREG_STATE_DIR"] = str(self.root / "state")
        os.environ["AUTOREG_MIGRATE_LEGACY"] = "0"
        sys.modules.pop("bot.config", None)
        self.config = importlib.import_module("bot.config")

    def tearDown(self) -> None:
        sys.modules.pop("bot.config", None)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_runtime_paths_share_external_state_root(self) -> None:
        state = (self.root / "state").resolve()
        self.assertEqual(self.config.STATE_DIR, state)
        self.assertEqual(self.config.DB_PATH, state / "tgbot.db")
        self.assertEqual(self.config.USER_DATA_DIR, state / "user_data")
        self.assertEqual(self.config.USER_PROXY_DIR, state / "user_proxies")
        self.assertEqual(self.config.JOB_PAYLOAD_DIR, state / "job_payloads")
        self.assertTrue(self.config.USER_DATA_DIR.is_dir())
        self.assertTrue(self.config.JOB_PAYLOAD_DIR.is_dir())

    def test_migration_helpers_copy_but_never_overwrite(self) -> None:
        source = self.root / "legacy.txt"
        destination = self.root / "state" / "legacy.txt"
        source.write_text("old-wallet", encoding="utf-8")

        self.assertTrue(self.config._copy_file_if_missing(source, destination))
        self.assertEqual(destination.read_text(encoding="utf-8"), "old-wallet")

        source.write_text("changed-source", encoding="utf-8")
        self.assertFalse(self.config._copy_file_if_missing(source, destination))
        self.assertEqual(destination.read_text(encoding="utf-8"), "old-wallet")

    def test_sqlite_migration_uses_verified_backup(self) -> None:
        source = self.root / "legacy.db"
        destination = self.root / "state" / "copied.db"
        with sqlite3.connect(source) as conn:
            conn.execute("CREATE TABLE sample(value TEXT)")
            conn.execute("INSERT INTO sample VALUES ('kept')")
            conn.commit()

        self.assertEqual(
            self.config._copy_sqlite_if_missing(source, destination),
            1,
        )
        with sqlite3.connect(destination) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM sample").fetchone()[0],
                "kept",
            )
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(
            self.config._copy_sqlite_if_missing(source, destination),
            0,
        )

    def test_legacy_compat_keeps_wallets_in_repo_data_dir(self) -> None:
        """A stale relative BOT_DB=tgbot.db must NOT make wallets vanish: the
        per-user dirs stay at the old BASE_DIR/data/* locations (and a warning
        is emitted). Only applies when AUTOREG_STATE_DIR is unset."""
        # setUp sets AUTOREG_STATE_DIR (so _LEGACY_COMPAT is False there).
        # Re-import config with a relative BOT_DB and no AUTOREG_STATE_DIR.
        # Keep the key explicitly empty so load_dotenv() cannot re-introduce
        # the developer machine's AUTOREG_STATE_DIR from the repository .env.
        os.environ["AUTOREG_STATE_DIR"] = ""
        for key in (
            "BOT_RESULTS",
            "PROXY_FILE",
            "SUBSCRIBERS_FILE",
            "USER_PROXY_DIR",
            "USER_DATA_DIR",
            "JOB_PAYLOAD_DIR",
        ):
            os.environ[key] = ""
        os.environ["BOT_DB"] = "tgbot.db"
        os.environ["AUTOREG_MIGRATE_LEGACY"] = "0"
        sys.modules.pop("bot.config", None)
        cfg = importlib.import_module("bot.config")
        try:
            self.assertTrue(cfg._LEGACY_COMPAT)
            self.assertTrue(
                cfg.USER_DATA_DIR == cfg.BASE_DIR / "data" / "user_data",
                cfg.USER_DATA_DIR,
            )
            self.assertTrue(
                cfg.USER_PROXY_DIR == cfg.BASE_DIR / "data" / "user_proxies",
                cfg.USER_PROXY_DIR,
            )
            # A custom (test) BOT_DB outside the checkout is NOT legacy.
            os.environ["BOT_DB"] = str(self.root / "custom" / "x.db")
            sys.modules.pop("bot.config", None)
            cfg2 = importlib.import_module("bot.config")
            self.assertFalse(cfg2._LEGACY_COMPAT)
        finally:
            os.environ.pop("BOT_DB", None)


if __name__ == "__main__":
    unittest.main()
