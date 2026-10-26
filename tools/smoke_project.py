#!/usr/bin/env python3
"""Live smoke of one project against a real `/data` profile, before it goes out.

Standing rule from the operator: a new project is tested on their own wallets
first, and the test only counts if the rows actually land on the project's
side — an `OK` in our log is a claim, not a receipt (PROJECTS.md §13).

    python tools/smoke_project.py <id> --user <your_telegram_id> --count 10
    python tools/smoke_project.py <id> --user <your_telegram_id> --count 10 --dry-run
    python tools/smoke_project.py <id> --user <your_telegram_id> --count 10 --verify-only

What it does:

1. reads `USER_DATA_DIR/<user>.txt` (`handle|wallet`) and takes the first N
   rows that the bot's **global wallet dedup** does not already consider done,
   so a repeat run never files the same wallet twice — the same check the
   queue makes before every row;
2. runs the project's own `submit` through the shared proxy pool, in the same
   thread pool shape the queue uses;
3. prints the status breakdown and the first distinct failure details;
4. **verifies** the OK rows where the project exposes a way to ask — a read
   endpoint, or a second idempotent call that answers "already". With
   `--verify-only` it asks about the wallets already filed for this project
   rather than the fresh ones, which is the whole point of asking later;
5. **records the results in the bot's database**, exactly as the queue records
   them. That is not bookkeeping — it is the point: a row filed here and not
   written down would be filed a second time the next time the operator
   registers everything, which on a project with no server-side dedup
   means a duplicate entry in its sheet. `--no-record` turns it off
   for a project you are only poking at.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "t:t")

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot.http_client import load_proxies  # noqa: E402
from bot.registry import get_project, load_project_folder, load_projects  # noqa: E402


def read_profile(user_id: int) -> list[tuple[str, str]]:
    path = Path(config.USER_DATA_DIR) / f"{user_id}.txt"
    if not path.exists():
        raise SystemExit(f"no /data profile at {path}")
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.replace(",", "|").split("|") if p.strip()]
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def pick_fresh(project_id: str, rows, count: int):
    """Rows the global dedup does not already call done — the queue's own rule."""
    fresh, skipped = [], 0
    for handle, wallet in rows:
        if len(fresh) >= count:
            break
        if bot_db.already_registered_wallet(project_id, wallet):
            skipped += 1
            continue
        fresh.append((handle, wallet))
    return fresh, skipped


# How to ask a project whether a wallet is really on its side. The adapter
# owns this knowledge: if `projects/<id>/api.py` exports
#     already_registered(wallet, proxy_url, handle="") -> (found, note)
# the smoke uses it. The legacy per-project map used to live here and was
# the same four delegates — moving it into the adapters means a new project
# brings its own checker along instead of a second file to remember.
#
# CAVEAT the adapter must state in its docstring: for several shapes
# (Supabase under RLS, GAS sheets, eligibility forms) this question IS a submission —
# it files the wallet when the list does not have it. Safe here, because
# the smoke only asks about rows the bot just filed.
def _verifier(project_id: str):
    try:
        module = __import__(f"projects.{project_id}.api", fromlist=["already_registered"])
    except ImportError:
        return None
    check = getattr(module, "already_registered", None)
    return check if callable(check) else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--user", type=int, default=int(os.getenv("OPERATOR_USER", "0") or 0))
    # 10, not 100: the operator's rule, cut to 20 and then to 10 on
    # 2026-08-20. A hundred rows say nothing the first ten did not, and against
    # a locked sheet they cost twenty minutes of wall clock to say it.
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--parallel", type=int, default=5)
    ap.add_argument("--verify", type=int, default=15, help="how many OK rows to verify")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, send nothing")
    ap.add_argument("--verify-only", action="store_true", help="skip submitting")
    ap.add_argument(
        "--no-record",
        action="store_true",
        help="do not write the results to the bot DB (they would then be filed again later)",
    )
    args = ap.parse_args()

    load_projects(force=True)
    project = get_project(args.project)
    if project is None:
        # A project being tested is still `enabled = false`, so the registry
        # skips it — that is the point of the order (PROJECTS.md шаг D/F.1).
        # Read it straight off disk instead of asking the operator to make it
        # visible to users first.
        project = load_project_folder(args.project)
        if project is not None:
            print(f"note      : {project.id} is disabled in project.toml — testing it off disk")
    if project is None:
        raise SystemExit(f"unknown project {args.project!r}")

    proxies = load_proxies(config.PROXY_FILE)
    if not proxies:
        raise SystemExit("no proxies loaded")

    rows = read_profile(args.user)
    fresh, skipped = pick_fresh(project.id, rows, args.count)
    print(f"project   : {project.id} ({project.name})")
    print(f"profile   : {len(rows)} wallets for user {args.user}")
    print(f"dedup     : {skipped} already registered -> skipped (a re-run would skip these)")
    print(f"to submit : {len(fresh)}")
    print(f"proxies   : {len(proxies)}")
    # `--verify-only` asks about rows that are already filed, so an empty fresh
    # list is its normal state — every wallet in the profile having been sent
    # is exactly when one wants to ask whether they landed.
    if args.dry_run or (not fresh and not args.verify_only):
        for handle, wallet in fresh[:5]:
            print(f"   would send {handle} {wallet}")
        return

    results = []
    if not args.verify_only:
        started = time.time()

        def one(item):
            index, (handle, wallet) = item
            fields = project.parse(f"{handle}|{wallet}") or {"handle": handle, "wallet": wallet}
            error = project.validate(fields)
            if error:
                return {"status": "BAD_RESP", "detail": f"validate: {error}", **fields}
            return project.submit(
                fields,
                proxies=proxies,
                proxy_idx=index,
                retries=4,
                retry_delay=3.0,
                min_delay=0.2,
                max_delay=0.8,
            )

        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
            results = list(pool.map(one, enumerate(fresh)))
        took = time.time() - started

        if not args.no_record:
            written = 0
            # strict: the two lists are built one per wallet a few lines apart. A
            # silent truncation here would record the wrong wallet's status.
            for (handle, wallet), result in zip(fresh, results, strict=True):
                status = str(result.get("status") or "FAIL")
                if status not in ("OK", "ALREADY"):
                    # Only settled rows go in: a network failure must stay
                    # retryable, exactly as the queue leaves it.
                    continue
                bot_db.record_submission(
                    args.user,
                    project.id,
                    result.get("handle") or handle,
                    result.get("wallet") or wallet,
                    status,
                    str(result.get("detail") or ""),
                )
                written += 1
            print(f"\n-- recorded {written} settled rows in the bot DB (dedup will skip them)")

        counts = collections.Counter(r.get("status", "?") for r in results)
        print(f"-- submitted {len(results)} rows in {took:.0f}s")
        for status, n in counts.most_common():
            print(f"   {status:<10} {n}")
        bad = collections.Counter(
            str(r.get("detail") or "")[:150]
            for r in results
            if r.get("status") not in ("OK", "ALREADY")
        )
        for detail, n in bad.most_common(5):
            print(f"   [{n}] {detail}")

    check = _verifier(project.id)
    if check is None:
        print("\n-- no read side on this project; `stored`/receipt in the body is all there is")
        return

    ok_rows = [r for r in results if r.get("status") == "OK"] if results else []
    if not ok_rows:
        # `--verify-only` asks about rows that were already filed, so it reads
        # the *sent* end of the profile rather than the fresh end. The fresh
        # rows were the wrong ones to pick for any project and an active
        # mistake for one whose checker is a submission (`demo-project`): asking
        # about an unsent wallet files it, and then answers "not found".
        sent = [(h, w) for h, w in rows if bot_db.already_registered_wallet(project.id, w)]
        if not sent:
            print("\n-- nothing filed for this project yet; nothing to verify")
            return
        ok_rows = [{"handle": h, "wallet": w} for h, w in sent]
    sample = ok_rows[: max(0, args.verify)]
    print(f"\n-- verifying {len(sample)} rows on the project's side")
    present = 0
    for i, r in enumerate(sample):
        try:
            found, note = check(
                r["wallet"], proxies[i % len(proxies)], handle=r.get("handle") or ""
            )
        except Exception as exc:  # noqa: BLE001
            found, note = False, f"probe error {type(exc).__name__}"
        present += bool(found)
        print(f"   {r['wallet'][:14]}… present={found}  {note[:90]}")
    print(f"   --> {present}/{len(sample)} confirmed on the project's side")


if __name__ == "__main__":
    main()
