"""The exit ledger has to survive a restart, not just a `/reload`.

`bot/exit_budget.py` keeps routes that count per IP from walking back into an
address they already spent. It used to hold that in RAM only, and the restart of
2026-08-20 16:26 showed the cost on demoo — one wallet per address, for good:
1% of rows lost the day before with a warm ledger, 21% after the restart, all of
them `This IP address / device has already registered`. These tests pin the two
halves of the fix: the ledger comes back from disk, and a windowed route gives
its allowance back instead of marking the pool spent for ever.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot import exit_budget  # noqa: E402

POOL = [f"http://user{i}:secretpass@gateway.example:8080" for i in range(6)]


def _shift(monkeypatch, seconds: float) -> None:
    """Move the ledger's clock forward without touching anyone else's."""
    real = exit_budget.time.time
    monkeypatch.setattr(exit_budget.time, "time", lambda: real() + seconds)


def _ledger(tmp_path, **kwargs):
    kwargs.setdefault("budget", 1)
    kwargs.setdefault("cooldown", 600.0)
    return exit_budget.ExitBudget("probe", path=tmp_path / "probe.json", **kwargs)


def test_spent_exits_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_budget, "SAVE_INTERVAL", 0.0)
    first = _ledger(tmp_path)
    spent = {first.pick(POOL, i) for i in range(len(POOL))}
    assert spent == set(POOL)  # budget=1: every exit handed out exactly once
    first.flush()

    # A new process reading the same file must not hand any of them back.
    second = _ledger(tmp_path)
    assert second.stats()["tracked"] == len(POOL)
    # Nothing is left with allowance, so `pick` falls back to the least-spent
    # exit rather than returning nothing — a request that may be refused still
    # beats no request.
    assert second.pick(POOL, 0) in POOL


def test_ledger_file_carries_no_gateway_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_budget, "SAVE_INTERVAL", 0.0)
    ledger = _ledger(tmp_path)
    ledger.pick(POOL, 0)
    ledger.flush()
    raw = (tmp_path / "probe.json").read_text(encoding="utf-8")
    assert "secretpass" not in raw
    assert "gateway.example" not in raw
    assert json.loads(raw)["uses"]  # it did record the exit, just not by name


def test_a_benched_exit_stays_benched_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_budget, "SAVE_INTERVAL", 0.0)
    first = _ledger(tmp_path, budget=3)
    first.penalise(POOL[0])
    first.flush()

    second = _ledger(tmp_path, budget=3)
    assert second.stats()["banned"] == 1
    # Six picks from a six-line pool never land on the benched one.
    assert POOL[0] not in {second.pick(POOL, i) for i in range(len(POOL))}


def test_a_windowed_budget_forgets_what_the_route_forgave(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_budget, "SAVE_INTERVAL", 0.0)
    first = _ledger(tmp_path, window=900.0)
    for i in range(len(POOL)):
        first.pick(POOL, i)
    first.flush()

    # Same file, read back after the window has passed: demon's throttle is
    # "try again in a few minutes", so those spends are no longer ours to keep.
    _shift(monkeypatch, 3600)
    later = _ledger(tmp_path, window=900.0)
    assert later.stats()["tracked"] == 0
    assert {later.pick(POOL, i) for i in range(len(POOL))} == set(POOL)


def test_a_permanent_budget_keeps_what_the_route_keeps(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_budget, "SAVE_INTERVAL", 0.0)
    first = _ledger(tmp_path)  # window=0 — demoo's kind
    first.pick(POOL, 0)
    first.flush()

    _shift(monkeypatch, 86400 * 40)
    later = _ledger(tmp_path)
    assert later.stats()["tracked"] == 1


def test_a_broken_file_is_an_empty_ledger_not_a_crash(tmp_path):
    (tmp_path / "probe.json").write_text("{not json", encoding="utf-8")
    ledger = _ledger(tmp_path)
    assert ledger.stats()["tracked"] == 0
    assert ledger.pick(POOL, 0) in POOL


def test_an_unwritable_path_never_breaks_a_row(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_budget, "SAVE_INTERVAL", 0.0)
    ledger = exit_budget.ExitBudget(
        "probe", budget=1, path=tmp_path / "no" / "such" / "dir" / "probe.json"
    )

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(exit_budget.Path, "mkdir", _boom)
    assert ledger.pick(POOL, 0) in POOL  # logged and swallowed
