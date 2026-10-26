"""Schema: base DDL plus numbered migrations with a version stamp.

History: this used to be ``_migrate(conn)`` in a 2.5k-line ``bot/db.py`` —
an append-only pile of ``PRAGMA table_info`` guards with no version table.
Every deployment ran every block on every boot, which was safe only because
each block was written to be idempotent, and fragile because nothing could
tell "already applied" from "never applied".

Now: ``schema_version`` lives in ``bot_settings``. A database with no stamp
(everything that exists today) is version 0 and runs every migration once,
then is stamped to the latest; a future migration is one more entry in
``MIGRATIONS`` and runs only on databases whose stamp is below it. Each step
keeps its old idempotency guards, so a stamp lost to a restored backup still
converges instead of crashing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from bot.config import FREE_OK_LIMIT
from bot.db import _now

SCHEMA_VERSION = 9
VERSION_KEY = "db_schema_version"


def _read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM bot_settings WHERE key = ?", (VERSION_KEY,)
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(str(row["value"]))
    except ValueError:
        return 0


def _stamp_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        """
        INSERT INTO bot_settings (key, value, updated_at, updated_by)
        VALUES (?, ?, ?, NULL)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (VERSION_KEY, str(int(version)), _now()),
    )
    conn.commit()


def _m1_users_columns(conn: sqlite3.Connection) -> None:
    """users.* columns added after the first release (lang, screen, ban, auto_reg)."""
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "selected_project" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN selected_project TEXT")
    if "banned" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0"
        )
    if "banned_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN banned_at TEXT")
    if "ban_reason" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
    if "lang" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'ru'"
        )
    # Anchored live screen: one editable message per user (see bot/screen.py)
    if "screen_chat_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN screen_chat_id INTEGER")
    if "screen_message_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN screen_message_id INTEGER")
    if "screen_state" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN screen_state TEXT NOT NULL DEFAULT ''"
        )
    if "screen_params" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN screen_params TEXT NOT NULL DEFAULT ''"
        )
    if "screen_updated_at" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN screen_updated_at TEXT NOT NULL DEFAULT ''"
        )
    # Telegram 403 «bot was blocked by the user» is permanent until the user
    # writes again — it is not a ban, so it needs a flag of its own. Kept out of
    # `banned` so an admin ban list stays an admin decision.
    if "blocked_bot" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN blocked_bot INTEGER NOT NULL DEFAULT 0"
        )
    # Auto-registration opt-in (per user, off by default): when a new project
    # appears, the bot queues a run for everyone who turned this on and has
    # /data uploaded. Off by default — starting somebody's job without their
    # consent spends their proxies and captcha balance.
    if "auto_reg" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN auto_reg INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()

def _m2_broadcast_audit_indexes(conn: sqlite3.Connection) -> None:
    """Durable broadcast outbox, admin audit, submissions time indexes."""
    # Durable broadcast queue: one row per recipient per batch. The sender is a
    # single thread, so a restart mid-batch resumes instead of losing or
    # doubling messages; the unique index is what makes a re-enqueue idempotent.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broadcast_outbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id        TEXT    NOT NULL,
            user_id         INTEGER NOT NULL,
            rank            INTEGER NOT NULL DEFAULT 0,
            body            TEXT    NOT NULL,
            parse_mode      TEXT    NOT NULL DEFAULT '',
            markup_json     TEXT    NOT NULL DEFAULT '',
            interval_sec    REAL    NOT NULL DEFAULT 0,
            state           TEXT    NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT    NOT NULL DEFAULT '',
            error           TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL,
            sent_at         TEXT    NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_batch_user
            ON broadcast_outbox(batch_id, user_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outbox_ready
            ON broadcast_outbox(state, rank, id)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_user_id INTEGER,
            detail TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_submissions_created
            ON submissions(created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_submissions_status_created
            ON submissions(status, created_at)
        """
    )
    conn.commit()

def _m3_submissions_columns(conn: sqlite3.Connection) -> None:
    """project_id / extra_json on submissions + legacy stats seed."""
    sub_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(submissions)").fetchall()
    }
    if "project_id" not in sub_cols:
        # Historical default for pre-multi-project DBs (removed project id; keep for row compat)
        conn.execute(
            "ALTER TABLE submissions ADD COLUMN project_id TEXT NOT NULL DEFAULT 'legacy_seed'"
        )
    if "extra_json" not in sub_cols:
        conn.execute("ALTER TABLE submissions ADD COLUMN extra_json TEXT")

    # Seed user_project_stats from legacy users.ok_count → a historical project id
    # (module removed; stats rows kept for history, not for active submit)
    legacy = conn.execute(
        "SELECT user_id, ok_count, updated_at FROM users WHERE ok_count > 0"
    ).fetchall()
    for row in legacy:
        exists = conn.execute(
            "SELECT 1 FROM user_project_stats WHERE user_id = ? AND project_id = ?",
            (row["user_id"], "legacy_seed"),
        ).fetchone()
        if exists is None:
            conn.execute(
                """
                INSERT INTO user_project_stats (user_id, project_id, ok_count, updated_at)
                VALUES (?, 'legacy_seed', ?, ?)
                """,
                (
                    row["user_id"],
                    int(row["ok_count"]),
                    row["updated_at"] or _now(),
                ),
            )
    conn.commit()

def _m4_mint_calendar(conn: sqlite3.Connection) -> None:
    """Mint calendar tables (see bot/mints.py; MINTS_ENABLED=0 today)."""
    # --- mint calendar (bot/mints.py) ---
    # Not in project.toml, though `mint_info` lives there. That field is a
    # sentence a human wrote once; this is a fact with a date that changes,
    # arrives from an outside source, and has to be confirmed before anyone
    # sees it. A file the deploy overwrites is the wrong home for that.
    #
    # Times are stored UTC and rendered in the reader's own zone: a calendar
    # that makes people convert in their head is a calendar people miss mints
    # by. `starts_at` NULL means announced-but-undated (TBA), which is a real
    # and common state and must not be confused with "no mint".
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_mints (
            project_id  TEXT PRIMARY KEY,
            supply      INTEGER,
            chain       TEXT    NOT NULL DEFAULT '',
            note        TEXT    NOT NULL DEFAULT '',
            source_url  TEXT    NOT NULL DEFAULT '',
            source      TEXT    NOT NULL DEFAULT 'manual',
            updated_at  TEXT    NOT NULL,
            updated_by  INTEGER
        )
        """
    )
    # `project.toml` has a `mint_url`, but in practice it holds the project's
    # home page — cativo's points at cativoworld.com while the mint is on
    # OpenSea. A button labelled "Минт" that opens a landing page is worse than
    # no button, so the real one is stored here where the operator can set it.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mint_phases (
            project_id  TEXT    NOT NULL,
            kind        TEXT    NOT NULL,
            starts_at   TEXT,
            price       TEXT    NOT NULL DEFAULT '',
            note        TEXT    NOT NULL DEFAULT '',
            updated_at  TEXT    NOT NULL,
            PRIMARY KEY (project_id, kind)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mint_phase_start
            ON mint_phases(starts_at)
        """
    )
    _mint_cols = {r[1] for r in conn.execute("PRAGMA table_info(project_mints)")}
    if "mint_url" not in _mint_cols:
        conn.execute("ALTER TABLE project_mints ADD COLUMN mint_url TEXT NOT NULL DEFAULT ''")
    # A drop worth tracking is not always a project the bot registers for.
    # Hood Citizens mints on OpenSea with no form to submit — it belongs on the
    # calendar and nowhere else — so a row may carry its own display name
    # instead of borrowing one from the registry.
    if "name" not in _mint_cols:
        conn.execute("ALTER TABLE project_mints ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    # Phase names come from the announcements themselves: Ratlist, Team,
    # Holder Claim. Five hardcoded kinds turned every one of them into "TBA",
    # which is the label for "no window given" — the opposite of a named one.
    _phase_cols = {r[1] for r in conn.execute("PRAGMA table_info(mint_phases)")}
    if "label" not in _phase_cols:
        conn.execute("ALTER TABLE mint_phases ADD COLUMN label TEXT NOT NULL DEFAULT ''")

    # Proposals waiting for the operator. Kept apart from the tables above so
    # that nothing unconfirmed can ever be read by the screen: the calendar
    # queries `mint_phases`, and only an accepted proposal writes there.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mint_proposals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT    NOT NULL,
            payload     TEXT    NOT NULL,
            quote       TEXT    NOT NULL DEFAULT '',
            source_url  TEXT    NOT NULL DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'pending',
            created_at  TEXT    NOT NULL,
            decided_at  TEXT,
            decided_by  INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mint_proposal_status
            ON mint_proposals(status, id)
        """
    )
    conn.commit()

def _m5_paid_plans(conn: sqlite3.Connection) -> None:
    """user_subscriptions + payments ledger (money is TEXT decimal strings)."""
    # --- paid plans (bot/billing.py) ---
    # Money is stored as TEXT decimal strings: pay_amount carries 8+ crypto
    # decimals and a payment ledger must never round-trip through REAL.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_subscriptions (
            user_id    INTEGER PRIMARY KEY,
            plan       TEXT    NOT NULL,
            expires_at TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT '',
            started_at TEXT    NOT NULL,
            updated_at TEXT    NOT NULL,
            updated_by INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_subs_expires
            ON user_subscriptions(expires_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            payment_id     TEXT PRIMARY KEY,
            order_id       TEXT NOT NULL UNIQUE,
            user_id        INTEGER NOT NULL,
            plan           TEXT NOT NULL,
            months         INTEGER NOT NULL DEFAULT 1,
            price_amount   TEXT NOT NULL,
            price_currency TEXT NOT NULL DEFAULT 'usd',
            pay_currency   TEXT NOT NULL DEFAULT 'usdtbsc',
            pay_address    TEXT NOT NULL DEFAULT '',
            pay_amount     TEXT NOT NULL DEFAULT '',
            actually_paid  TEXT NOT NULL DEFAULT '',
            status         TEXT NOT NULL DEFAULT 'waiting',
            chat_id        INTEGER,
            message_id     INTEGER,
            valid_until    TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            last_poll_at   TEXT NOT NULL DEFAULT '',
            poll_attempts  INTEGER NOT NULL DEFAULT 0,
            credited_at    TEXT,
            detail         TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_payments_open
            ON payments(status, last_poll_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_payments_user
            ON payments(user_id, created_at)
        """
    )
    conn.commit()

def _m6_drop_own_limit(conn: sqlite3.Connection) -> None:
    # The own-proxy tier was removed on 2026-09-09 (operator rule: uploading
    # /proxy changes only the traffic source, never the OK limit). Databases
    # seeded before that still carry a stale `own_limit` row that nothing
    # reads anymore; drop it so /limits_config and the settings table stop
    # advertising a knob that no longer exists.
    conn.execute("DELETE FROM bot_settings WHERE key = 'own_limit'")
    conn.commit()


def _m7_queue_lease(conn: sqlite3.Connection) -> None:
    # Persistent queue groundwork: lease columns on submit_jobs and the
    # submit_job_rows table (payload files move into the same transaction as
    # the job). No-op when submit_jobs does not exist yet — it is created by
    # submit_queue.init_schema, which runs the same ensure_schema after its
    # CREATE TABLE, so both orders converge.
    from bot.db.queue_jobs import ensure_schema

    ensure_schema(conn)


def _m8_mint_remind(conn: sqlite3.Connection) -> None:
    """users.mint_remind — opt-in T-1h mint reminders (NFT Trencher feed).

    Off by default: reminders go out only to people who asked, and the feed
    itself is keyed (NFTT_API_KEY) — a column of zeros costs nothing while
    either half is dark.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "mint_remind" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN mint_remind INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def _m9_funnel_events(conn: sqlite3.Connection) -> None:
    """приватка funnel: one row per menu tap / view / 5/5 win line."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS funnel_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          event TEXT NOT NULL,
          branch TEXT DEFAULT '',
          ts INTEGER NOT NULL
        )
        """
    )
    conn.commit()


# (number, callable) — append, never renumber. A migration older than the
# database's stamp is skipped; one newer runs once and bumps the stamp.
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _m1_users_columns),
    (2, _m2_broadcast_audit_indexes),
    (3, _m3_submissions_columns),
    (4, _m4_mint_calendar),
    (5, _m5_paid_plans),
    (6, _m6_drop_own_limit),
    (7, _m7_queue_lease),
    (8, _m8_mint_remind),
    (9, _m9_funnel_events),
]


def _seed_limit_settings(conn: sqlite3.Connection) -> None:
    now = _now()
    for key, val in (
        ("free_limit", str(int(FREE_OK_LIMIT))),
    ):
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO bot_settings (key, value, updated_at, updated_by)
                VALUES (?, ?, ?, NULL)
                """,
                (key, val, now),
            )
    conn.commit()

def apply_schema(conn: sqlite3.Connection) -> None:
    """Create the base tables, run pending migrations, then late indexes.

    Base DDL first (fresh databases need the tables before migrations can
    PRAGMA-check them), migrations in number order against the version stamp,
    and the dedup/index work last because some of it needs migrated columns.
    """
    conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT,
                    first_name  TEXT,
                    ok_count    INTEGER NOT NULL DEFAULT 0,
                    selected_project TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_project_stats (
                    user_id     INTEGER NOT NULL,
                    project_id  TEXT    NOT NULL,
                    ok_count    INTEGER NOT NULL DEFAULT 0,
                    updated_at  TEXT    NOT NULL,
                    PRIMARY KEY (user_id, project_id)
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    project_id  TEXT    NOT NULL DEFAULT '',
                    handle      TEXT,
                    wallet      TEXT,
                    extra_json  TEXT,
                    status      TEXT    NOT NULL,
                    detail      TEXT,
                    created_at  TEXT    NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                CREATE TABLE IF NOT EXISTS project_suggestions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    username    TEXT,
                    first_name  TEXT,
                    body        TEXT    NOT NULL,
                    created_at  TEXT    NOT NULL,
                    utc_day     TEXT    NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sub_user ON submissions(user_id);
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by INTEGER
                );
                CREATE TABLE IF NOT EXISTS user_limit_overrides (
                    user_id INTEGER PRIMARY KEY,
                    ok_limit INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by INTEGER
                );
                """

    )
    version = _read_version(conn)
    for number, migrate in MIGRATIONS:
        if number <= version:
            continue
        migrate(conn)
        _stamp_version(conn, number)
    # Migrate old DBs before indexes that need new columns
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sub_user_project
            ON submissions(user_id, project_id)
        """
    )
    # Dedup lookups: local (user+project+wallet) and global (project+wallet)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sub_user_proj_wallet
            ON submissions(user_id, project_id, wallet)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sub_proj_wallet
            ON submissions(project_id, wallet)
        """
    )
    # The dedup query compares `lower(wallet)`, because wallets arrive
    # in whatever case the user typed. A function around the column
    # means the index above can only be used for its first term, so
    # every check scanned all of that project's rows — ten thousand and
    # growing with every registration. Indexing the expression makes it
    # a seek: measured on the live database, 3.0 ms to 0.5 ms per
    # project, which across a full project list is the difference
    # between 72 ms and 12 ms on every screen refresh. Costs 11 MB.
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sub_proj_lwallet
            ON submissions(project_id, lower(wallet), status)
        """
    )
    # In-flight wallet claims (AUD-007 race with global dedup)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_claims (
            project_id  TEXT NOT NULL,
            wallet      TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (project_id, wallet)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ok_slot_reservations (
            reservation_id TEXT PRIMARY KEY,
            user_id        INTEGER NOT NULL,
            project_id     TEXT NOT NULL,
            created_at     TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ok_slot_user_project
            ON ok_slot_reservations(user_id, project_id)
        """
    )
    # One suggestion per user per UTC day (enforced at DB level)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_suggest_user_day_unique
            ON project_suggestions(user_id, utc_day)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS suggest_pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            -- scanned|refused|drafted|import_failed|probe_failed|tested|error
            status TEXT NOT NULL,
            report TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (suggestion_id, url)
        )
        """
    )
    _seed_limit_settings(conn)
    conn.commit()
