"""Guard against semantic drift between docs, config and the tier ladder.

This repo is edited by AI agents, and the documentation is effectively part
of the executable system: a stale PROJECTS/AGENTS claim becomes wrong code
sooner or later. These invariants pin the removals that already happened:

* the own-proxy tier (2026-09-09) — no knob, no env, no DB function;
* the Pro plan (2026-08-17) — no PLAN_PRO, no "pro" tier mode;
* every i18n key exists in both RU and EN.
"""

from __future__ import annotations

import re
from pathlib import Path

from bot import i18n

BASE = Path(__file__).resolve().parent.parent

# Identifiers that must never come back (word-boundary match on source).
FORBIDDEN_IDENTIFIERS = (
    r"\bOWN_PROXY_OK_LIMIT\b",
    r"\bset_own_limit\b",
    r"\bget_own_limit\b",
    r"\bown_limit\b",
    r"\bPLAN_PRO\b",
)

# bot/ is the runtime source of truth; tests and broadcast's TIER_RANK keep
# deliberate legacy string keys, so they are not scanned.
SCAN_GLOBS = ("bot/*.py", "bot/db/*.py", "handlers/*.py")

# Docs are executable input for the agents that edit this repo: a stale
# tier table becomes wrong code on the next edit. Same identifiers, plus
# phrases that only existed for the retired own/pro tiers.
DOC_GLOBS = ("AGENTS.md", "PROJECTS.md", "ops/*.md", ".env.example", "webapp/README.md")
FORBIDDEN_DOC_PHRASES = FORBIDDEN_IDENTIFIERS + (
    r"free/own",
    r"own ?= ?50",
    r"own=50",
    r"\$2 / 30",
    r"\$10 / 30",
    r"inherit Pro",
)

# A line that documents the removal itself is allowed to name what it
# removed ("the own_limit knob was removed", migration notes).
_REMOVAL_CONTEXT = re.compile(
    r"removed|deletes the stale|retired|миграц|was the last", re.IGNORECASE
)


def test_no_own_proxy_tier_residue() -> None:
    patterns = [re.compile(p) for p in FORBIDDEN_IDENTIFIERS]
    offenders: list[str] = []
    for glob in SCAN_GLOBS:
        for path in sorted(BASE.glob(glob)):
            text = path.read_text(encoding="utf-8")
            # The removal itself is allowed to name what it removes: the
            # migration that drops the row and comments saying it is gone.
            # Occurrences inside quotes and comment lines are therefore fine;
            # live identifiers (config constants, DB functions) are not.
            text = text.replace("'own_limit'", "").replace('"own_limit"', "")
            for line in text.splitlines():
                if "own_limit" in line and line.lstrip().startswith("#"):
                    continue
                for pat in patterns:
                    m = pat.search(line)
                    if m:
                        offenders.append(
                            f"{path.relative_to(BASE)}: {m.group(0)} in {line.strip()[:80]}"
                        )
    assert not offenders, "retired own-proxy/pro residue found:\n" + "\n".join(offenders)


def test_no_own_proxy_residue_in_docs() -> None:
    patterns = [re.compile(p) for p in FORBIDDEN_DOC_PHRASES]
    offenders: list[str] = []
    for glob in DOC_GLOBS:
        for path in sorted(BASE.glob(glob)):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if _REMOVAL_CONTEXT.search(line):
                    continue
                for pat in patterns:
                    m = pat.search(line)
                    if m:
                        offenders.append(
                            f"{path.relative_to(BASE)}:{lineno}: "
                            f"{m.group(0)} in {line.strip()[:70]}"
                        )
    assert not offenders, "retired own/pro tier documented as live:\n" + "\n".join(
        offenders
    )


def test_no_pro_tier_mode() -> None:
    # limit_mode() answers one of exactly these; "pro"/"own" are gone.
    allowed = {"vip", "basic", "subscriber", "free"}
    src = (BASE / "bot" / "limits.py").read_text(encoding="utf-8")
    assert "mode=plan.id" in src or "mode=\"" in src
    for key in i18n.STRINGS:
        if key.startswith("tier."):
            assert key[len("tier."):] in allowed, f"i18n has retired tier label {key}"


def test_i18n_key_language_parity() -> None:
    missing: list[str] = []
    for key, vals in i18n.STRINGS.items():
        for lang in i18n.SUPPORTED:
            v = vals.get(lang, "")
            if not str(v).strip():
                missing.append(f"{key}: empty {lang}")
    assert not missing, "i18n keys missing a language:\n" + "\n".join(missing)
    assert len(i18n.STRINGS) > 100, "i18n dictionary unexpectedly small"


def main() -> None:
    # The legacy `python -m tests.test_invariants` runner would import this
    # module, call nothing and exit 0 — a green checkmark that verified
    # nothing. Refuse loudly instead of passing vacuously.
    raise SystemExit(
        "tests/test_invariants.py is pytest-only (plain test_* functions). "
        "Run it with: python -m pytest tests/test_invariants.py -q"
    )


if __name__ == "__main__":
    main()
