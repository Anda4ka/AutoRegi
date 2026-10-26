"""A captcha-backed project is offered to **VIP only**.

Every row of such a project spends the operator's own solver balance, so the
gate is admin-granted status (`is_vip`) and nothing else. It used to include
paying tiers; the operator narrowed it on 2026-08-21, and the reason is worth
keeping: a $50 plan buys OK slots, not an open tap on somebody else's
Capsolver/2Captcha account — a thousand-row job at ~$0.0015 a token spends real
money nobody agreed to. So free, own-proxy, `active_subscribers.txt` **and
basic/pro payers** must not even see it in the picker.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot import limits as bot_limits
from bot.registry import _names_a_captcha_key


def _project(pid: str, *, captcha: bool):
    return SimpleNamespace(id=pid, requires_captcha=captcha, min_ok_limit=0)


def _as_mode(monkeypatch, mode: str, *, plan: str = "", vip: bool = False) -> None:
    caps = SimpleNamespace(mode=mode, plan=plan, ok_limit=50)
    monkeypatch.setattr(bot_limits, "tier_caps", lambda *_a, **_k: caps)
    monkeypatch.setattr(
        bot_limits.billing, "active_plan", lambda _uid: (plan, "") if plan else None
    )
    # The gate reads admin-granted status, which lives in SQLite; the tier is
    # irrelevant to it and is stubbed here only so the rest of `project_allowed`
    # has something to read.
    monkeypatch.setattr(bot_limits, "is_vip", lambda _uid: vip)


def test_vip_sees_a_captcha_project(monkeypatch) -> None:
    _as_mode(monkeypatch, "vip", vip=True)
    assert bot_limits.project_allowed(_project("mohoo", captcha=True), 1)


def test_paying_tiers_do_not(monkeypatch) -> None:
    """The rule the operator narrowed: paying is not the same as being trusted
    with the solver balance."""
    _as_mode(monkeypatch, "basic", plan="basic", vip=False)
    assert not bot_limits.project_allowed(_project("mohoo", captcha=True), 1)
    # …and it is only the captcha gate that shuts them out, not the picker.
    assert bot_limits.project_allowed(_project("cultnft", captcha=False), 1)


def test_unpaid_tiers_do_not(monkeypatch) -> None:
    for mode in ("free", "subscriber"):
        _as_mode(monkeypatch, mode, vip=False)
        assert not bot_limits.project_allowed(_project("mohoo", captcha=True), 1), mode


def test_a_subscriber_file_entry_does_not_buy_captcha(monkeypatch) -> None:
    """`active_subscribers.txt` raises the OK limit, not the solver balance."""
    _as_mode(monkeypatch, "subscriber", vip=False)
    assert not bot_limits.project_allowed(_project("demoq", captcha=True), 42)
    assert bot_limits.project_allowed(_project("cultnft", captcha=False), 42)


def test_a_vip_whose_tier_reads_lower_still_counts(monkeypatch) -> None:
    """VIP is a grant, not a rung: a VIP override reading as 'subscriber' still passes."""
    _as_mode(monkeypatch, "subscriber", vip=True)
    assert bot_limits.project_allowed(_project("mohoo", captcha=True), 7)


def test_anonymous_never_sees_a_captcha_project(monkeypatch) -> None:
    _as_mode(monkeypatch, "free", vip=False)
    assert not bot_limits.project_allowed(_project("mohoo", captcha=True), None)


def test_visible_projects_filters_the_list(monkeypatch) -> None:
    _as_mode(monkeypatch, "free", vip=False)
    projects = [_project("cultnft", captcha=False), _project("mohoo", captcha=True)]
    assert [p.id for p in bot_limits.visible_projects(projects, 5)] == ["cultnft"]


def test_solver_key_marks_a_project_even_without_the_flag() -> None:
    """Forgetting `requires_captcha` must not leak the balance to free users."""
    assert _names_a_captcha_key(["CAPSOLVER_API_KEY"])
    assert _names_a_captcha_key(["TWOCAPTCHA_API_KEY"])  # matched on the name
    assert _names_a_captcha_key(["MY_TURNSTILE_TOKEN"])
    assert not _names_a_captcha_key(["SUPABASE_KEY"])
    assert not _names_a_captcha_key([])
