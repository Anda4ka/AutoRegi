#!/usr/bin/env python3
"""One-shot checklist for adding a project (PROJECTS.md §3, шаги A–F.1).

Each step is one of the two tools that already exist; this only chains them
and refuses to go on when a gate fails, so the order in PROJECTS.md is what
actually happens instead of what the document says:

    python tools/new_project.py https://example.xyz/apply myproj
        1. scaffold --init   — recon + draft projects/<id>/ from a template
        2. edit gate         — nothing runs until api.py is written by hand
        3. smoke --dry-run   — registry load, parse/validate, dedup plan
        4. smoke --live      — 10 operator wallets (the operator's rule),
                               optional, explicit: this files real rows

What it never does: set `enabled = true`, restart the bot, or send the
announcement — those stay human steps (PROJECTS.md шаги G and G.7).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPERATOR_USER = int(os.getenv("OPERATOR_USER", "0") or 0)


def run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(cmd)}\n")
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("project_id")
    ap.add_argument("--user", type=int, default=OPERATOR_USER)
    ap.add_argument("--count", type=int, default=10,
                    help="operator rule since 2026-08-20: 10, not 100")
    ap.add_argument("--live", action="store_true",
                    help="also run the live smoke — files real rows")
    args = ap.parse_args()

    if not (ROOT / "tools" / "scaffold_project.py").exists():
        raise SystemExit("run from the repo root")

    folder = ROOT / "projects" / args.project_id
    if folder.exists() and (folder / "module.py").exists():
        print(f"-- projects/{args.project_id} already has module.py — skipping scaffold\n")
    else:
        code = run([sys.executable, "tools/scaffold_project.py",
                    args.url, "--id", args.project_id, "--init"])
        if code != 0:
            return code
        print("\n== ШАГ 2: допиши адаптер руками — скрипт не угадает успех/ALREADY")
        print(f"   правь projects/{args.project_id}/api.py и module.py, потом запусти снова")
        print("   (повторный запуск продолжит с шага 3)")
        return 0

    code = run([sys.executable, "tools/smoke_project.py",
                args.project_id, "--user", str(args.user),
                "--count", str(args.count), "--dry-run"])
    if code != 0:
        return code

    if not args.live:
        print("\n-- dry-run прошёл. Живой прогон (10 тестовых кошельков):")
        print(f"   python tools/new_project.py {args.url} {args.project_id} --live")
        return 0

    code = run([sys.executable, "tools/smoke_project.py",
                args.project_id, "--user", str(args.user),
                "--count", str(args.count)])
    if code != 0:
        return code

    print("\n== Дальше — только руками (PROJECTS.md шаги G–G.7):")
    print(f"   1. enabled = true в projects/{args.project_id}/project.toml")
    print("      (перед этим: twitter_url, mint_info — анонс рендерится из них)")
    print("   2. рестарт бота или /reload")
    print("   3. превью анонса придёт админам — прочитать до «Разослать»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
