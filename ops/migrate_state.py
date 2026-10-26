#!/usr/bin/env python3
"""Recover ignored runtime state from an older autoreg checkout.

The destination is never overwritten. Stop the bot before migrating SQLite.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

FILE_MAP = {
    "tgbot.db": "tgbot.db",
    "tgbot.db-wal": "tgbot.db-wal",
    "tgbot.db-shm": "tgbot.db-shm",
    "bot_results.jsonl": "bot_results.jsonl",
    "proxy.txt": "proxy.txt",
    "active_subscribers.txt": "active_subscribers.txt",
}
DIR_MAP = {
    "data/user_data": "user_data",
    "data/user_proxies": "user_proxies",
    "data/job_payloads": "job_payloads",
}


def copy_missing(src: Path, dst: Path, *, dry_run: bool) -> str:
    if not src.is_file():
        return "missing"
    if dst.exists():
        return "kept-existing"
    if dry_run:
        return "would-copy"
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.migrate-{os.getpid()}")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return "copied"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--from-root",
        required=True,
        type=Path,
        help="Old checkout containing tgbot.db and data/user_data",
    )
    parser.add_argument(
        "--to",
        type=Path,
        default=Path.home() / ".autoreg",
        help="Persistent state directory (default: ~/.autoreg)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    src_root = args.from_root.expanduser().resolve()
    dst_root = args.to.expanduser().resolve()
    if not src_root.is_dir():
        parser.error(f"source checkout does not exist: {src_root}")
    if src_root == dst_root:
        parser.error("source and destination must differ")

    counts: dict[str, int] = {}
    for src_rel, dst_rel in FILE_MAP.items():
        status = copy_missing(
            src_root / src_rel, dst_root / dst_rel, dry_run=args.dry_run
        )
        counts[status] = counts.get(status, 0) + 1
        print(f"{status:14} {src_rel} -> {dst_root / dst_rel}")

    for src_rel, dst_rel in DIR_MAP.items():
        src_dir = src_root / src_rel
        if not src_dir.is_dir():
            print(f"missing        {src_rel}")
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        for src in sorted(src_dir.rglob("*")):
            if not src.is_file() or src.name == ".gitkeep":
                continue
            rel = src.relative_to(src_dir)
            dst = dst_root / dst_rel / rel
            status = copy_missing(src, dst, dry_run=args.dry_run)
            counts[status] = counts.get(status, 0) + 1
            print(f"{status:14} {src.relative_to(src_root)} -> {dst}")

    print("Summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not args.dry_run:
        print(f"Persistent state ready at: {dst_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
