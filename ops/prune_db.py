#!/usr/bin/env python3
"""Drop rows that belong to projects the bot no longer serves.

A project_id is *live* when `projects/<id>/project.toml` exists and says
`enabled = true`. Everything else — disabled, deleted from the tree, or a typo
that only ever existed in the database — is prunable once its rows are older
than the retention window.

Live projects are never touched, whatever their age: `submissions` backs the
global wallet dedup (`bot/db.already_registered_wallet`), so deleting a live
project's history would let already-registered wallets through a second time.

Usage:
    ops/prune_db.py                     # dry run, prints what it would delete
    ops/prune_db.py --apply             # delete, after an automatic backup
    ops/prune_db.py --days 30 --apply   # different retention window
    ops/prune_db.py --apply --vacuum    # also reclaim the freed pages

Cron (daily 05:00):
    0 5 * * * /opt/autoreg/venv/bin/python /opt/autoreg/ops/prune_db.py --apply >> /var/lib/autoreg/prune_db.log 2>&1
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(
    os.getenv("BOT_DB")
    or Path(os.getenv("AUTOREG_STATE_DIR", "~/.autoreg")).expanduser() / "tgbot.db"
).expanduser()

# table -> column carrying the row's age. ok_slot_reservations is deliberately
# absent: it is live queue bookkeeping cleared on boot, not history.
TABLES = {
    "submissions": "created_at",
    "submit_jobs": "created_at",
    "user_project_stats": "updated_at",
    "wallet_claims": "created_at",
}


def live_project_ids(projects_dir: Path) -> set[str]:
    out: set[str] = set()
    if not projects_dir.is_dir():
        return out
    try:
        folders = sorted(projects_dir.iterdir())
    except OSError as exc:
        print(f"[prune] cannot list {projects_dir}: {exc}")
        return out
    for folder in folders:
        try:
            if not folder.is_dir() or folder.name.startswith(("_", ".")):
                continue
            toml_path = folder / "project.toml"
            if not toml_path.is_file():
                continue
        except OSError as exc:
            # Unreadable entry: cannot prove it is dead, so do not risk it.
            print(f"[prune] WARN cannot stat {folder}: {exc} — treating as live")
            out.add(folder.name)
            continue
        try:
            meta = tomllib.loads(toml_path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            # A project we cannot read is treated as live: never delete on a
            # parse error.
            print(f"[prune] WARN unreadable {toml_path}: {exc} — treating as live")
            out.add(folder.name)
            continue
        if meta.get("enabled"):
            out.add(str(meta.get("id") or folder.name))
    return out


def backup(db: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = db.with_name(f"{db.stem}.pre-prune-{stamp}{db.suffix}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(dest)
        try:
            con.backup(target)      # consistent even while the bot is running
        finally:
            target.close()
    finally:
        con.close()
    os.chmod(dest, 0o600)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--projects", type=Path, default=ROOT / "projects")
    ap.add_argument("--days", type=int, default=7, help="retention window (default 7)")
    ap.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    ap.add_argument("--vacuum", action="store_true", help="VACUUM after deleting")
    ap.add_argument("--keep-backups", type=int, default=3,
                    help="pre-prune backups to keep (default 3)")
    args = ap.parse_args()

    if args.days < 1:
        print("[prune] --days must be >= 1", file=sys.stderr)
        return 2
    if not args.db.is_file():
        print(f"[prune] no database at {args.db}", file=sys.stderr)
        return 1

    live = live_project_ids(args.projects)
    if not live:
        # An empty tree almost certainly means a wrong --projects path, and
        # would otherwise mark every row prunable.
        print(f"[prune] refusing: no live projects found under {args.projects}",
              file=sys.stderr)
        return 1

    cutoff = f"-{args.days} day"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[prune] {stamp} db={args.db} live={len(live)} retention={args.days}d "
          f"mode={'APPLY' if args.apply else 'dry-run'}")

    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(live))
        params = sorted(live)
        plan: list[tuple[str, str, int]] = []
        for table, date_col in TABLES.items():
            rows = con.execute(
                f"SELECT project_id, COUNT(*) n FROM {table} "
                f"WHERE project_id NOT IN ({placeholders}) "
                f"AND {date_col} < datetime('now', ?) "
                f"GROUP BY project_id ORDER BY n DESC",
                (*params, cutoff),
            ).fetchall()
            for r in rows:
                plan.append((table, r["project_id"], r["n"]))

        total = sum(n for _t, _p, n in plan)
        if not plan:
            print(f"[prune] nothing older than {args.days}d for dead projects — 0 rows")
            return 0
        width = max(len(t) for t, _p, _n in plan)
        for table, pid, n in plan:
            print(f"[prune]   {table:<{width}}  {pid:<20} {n:>7}")
        print(f"[prune] total {total} row(s) across {len({p for _t, p, _n in plan})} dead project(s)")

        if not args.apply:
            print("[prune] dry run — nothing deleted (pass --apply)")
            return 0

        saved = backup(args.db)
        print(f"[prune] backup {saved} ({saved.stat().st_size // 1024} KB)")

        deleted = 0
        for table, date_col in TABLES.items():
            cur = con.execute(
                f"DELETE FROM {table} "
                f"WHERE project_id NOT IN ({placeholders}) "
                f"AND {date_col} < datetime('now', ?)",
                (*params, cutoff),
            )
            deleted += cur.rowcount
        con.commit()
        print(f"[prune] deleted {deleted} row(s)")

        if args.vacuum:
            before = args.db.stat().st_size
            con.execute("VACUUM")
            after = args.db.stat().st_size
            print(f"[prune] vacuum {before // 1024} KB -> {after // 1024} KB")
    finally:
        con.close()

    # Keep the backup directory from growing without bound.
    backups = sorted(args.db.parent.glob(f"{args.db.stem}.pre-prune-*{args.db.suffix}"))
    for old in backups[: max(0, len(backups) - args.keep_backups)]:
        old.unlink()
        print(f"[prune] removed old backup {old.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
