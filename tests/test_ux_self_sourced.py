"""Project screen for self-sourced projects.

A self-sourced project has nothing to upload and spends a whole batch of slots
per tap, so its screen differs from every other project: no /data prompts, and
Start goes through a confirmation. Written for DigiDigi and kept when that
project was removed on 15.08.2026 — every fixture
here builds its own `Project`, so the screen rules outlive the example, and
the next self-sourced project inherits the coverage.

Named `test_ux_*` on purpose: the other tgbot-importing suites re-import the
`bot.*` package under their own env, and whoever imports `tgbot` first fixes
its bindings. Sorting after them keeps that order, and every fixture below
seeds through the modules `tgbot` itself holds rather than a fresh import, so
the state written is the state the screen reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tgbot
from bot.registry import Project

bot_db = tgbot.bot_db
bot_limits = tgbot.bot_limits
ud = tgbot.user_data_mod
# Same object tgbot reads (phase-3 split moved the pool state out of tgbot).
proxy_pool = tgbot.proxy_pool

UID = 881001
BATCH = 100


def _ok(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _set_proxies(pool) -> None:
    # Pool state lives in bot/proxy_pool.py (phase-3 split). Reach it through
    # the tgbot binding — same module generation as the code under test.
    tgbot.proxy_pool._proxies_loaded = True
    tgbot.proxy_pool._proxies[:] = list(pool)


def _user(uid: int, limit: int, *, accounts: int = 0) -> None:
    bot_db.ensure_user(uid, "u", "U")
    bot_db.set_user_limit_override(uid, limit, admin_id=1)
    bot_limits.invalidate(uid)
    if accounts:
        data = "\n".join(f"u{i}|0x" + ("%02x" % (i + 1)) * 20 for i in range(accounts))
        ud.save_user_data_from_bytes(uid, data.encode("utf-8"))
    else:
        ud.clear_user_data(uid)


def _project(**kw) -> Project:
    defaults = {
        "id": "selfui",
        "name": "Self UI",
        "ok_limit": 100,
        "order": 1,
        "description": "",
        "help_html": lambda: "help text",
        "parse": lambda _t: None,
        "validate": lambda _f: None,
        "submit": lambda *_a, **_k: {"status": "OK"},
        "self_sourced": True,
        "generate_rows": lambda **_k: [],
        "batch_size": BATCH,
        "min_ok_limit": BATCH,
    }
    defaults.update(kw)
    return Project(**defaults)


def _classic() -> Project:
    return _project(
        id="classicui", name="Classic", self_sourced=False, generate_rows=None,
        batch_size=0, min_ok_limit=0,
        bulk_columns=[("handle", True, "twitter handle")],
    )


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.keyboard for b in row if b.callback_data]


# --------------------------------------------------------------- start state

def test_no_upload_needed() -> None:
    """The whole /data gate is bypassed — the project mints its own rows."""
    _set_proxies(["http://u:p@host:1"])
    _user(UID, 100, accounts=0)
    st = tgbot._project_start_state(_project(), 0, UID)
    _ok(st["self_sourced"] is True, "flagged self-sourced")
    _ok(st["has_data"] is True, "must not demand an upload")
    _ok(st["will_send"] == BATCH, f"will_send should be the batch, got {st['will_send']}")
    _ok(st["can_start"] is True, "a full batch of slots means it can start")


def test_partial_batch_cannot_start() -> None:
    """All-or-nothing: 99 slots must not launch a 100-wallet batch."""
    _set_proxies(["http://u:p@host:1"])
    _user(UID + 1, BATCH - 1)
    st = tgbot._project_start_state(_project(), 0, UID + 1)
    _ok(st["can_start"] is False, "a partial batch must not be startable")
    _ok(st["left"] == BATCH - 1, f"left {st['left']}")


def test_spent_slots_close_the_batch() -> None:
    """Slots already used on this project count against the next batch."""
    _set_proxies(["http://u:p@host:1"])
    uid = UID + 2
    _user(uid, BATCH)
    project = _project()
    st = tgbot._project_start_state(project, 1, uid)
    _ok(st["can_start"] is False, "99 free slots cannot run a 100 batch")


def test_no_proxies_still_blocks() -> None:
    """Minting wallets does not exempt a project from the proxy rule."""
    _set_proxies([])
    _user(UID + 3, 100)
    st = tgbot._project_start_state(_project(), 0, UID + 3)
    _ok(st["can_start"] is False, "no proxy pool means no start")


def test_classic_project_state_unchanged() -> None:
    """Regression: the /data path must behave exactly as before."""
    _set_proxies(["http://u:p@host:1"])
    uid = UID + 4
    _user(uid, 100, accounts=3)
    st = tgbot._project_start_state(_classic(), 0, uid)
    _ok(st["self_sourced"] is False, "classic project is not self-sourced")
    _ok(st["has_data"] is True and st["n_data"] == 3, f"data rows {st['n_data']}")
    _ok(st["will_send"] == 3, f"sends what was uploaded, got {st['will_send']}")

    _user(uid, 100, accounts=0)
    st = tgbot._project_start_state(_classic(), 0, uid)
    _ok(st["has_data"] is False, "no upload → still demands one")
    _ok(st["can_start"] is False, "cannot start without data")


# -------------------------------------------------------------------- screen

def test_screen_text_talks_about_wallets_not_uploads() -> None:
    _set_proxies(["http://u:p@host:1"])
    _user(UID + 5, 100)
    text = tgbot.project_ready_text(_project(), 0, UID + 5, lang="ru")
    _ok(str(BATCH) in text, "the batch size is stated")
    _ok("загружать ничего не нужно" in text.lower(), f"missing self-sourced line:\n{text}")
    _ok("сначала загрузите аккаунты" not in text.lower(), "must not ask for an upload")


def test_screen_text_explains_missing_slots() -> None:
    _set_proxies(["http://u:p@host:1"])
    _user(UID + 6, 40)
    text = tgbot.project_ready_text(_project(), 0, UID + 6, lang="ru")
    _ok("свободных слотов" in text.lower(), f"missing slot hint:\n{text}")
    _ok("40" in text, "states how many are left")


# ------------------------------------------------------------------ keyboard

def test_keyboard_asks_before_starting() -> None:
    """One tap spends 100 slots, so the first tap only opens a confirmation."""
    _set_proxies(["http://u:p@host:1"])
    _user(UID + 7, 100)
    project = _project()
    kb = tgbot.project_screen_keyboard(project, lang="ru", user_id=UID + 7, ok_count=0)
    cbs = _callbacks(kb)
    _ok(f"queue:ask:{project.id}" in cbs, f"expected a confirm step, got {cbs}")
    _ok(f"queue:start:{project.id}" not in cbs, "must not start on the first tap")


def test_keyboard_hides_account_buttons() -> None:
    """Nothing to upload or edit — those buttons would be dead ends."""
    _set_proxies(["http://u:p@host:1"])
    _user(UID + 8, 100)
    kb = tgbot.project_screen_keyboard(_project(), lang="ru", user_id=UID + 8, ok_count=0)
    cbs = _callbacks(kb)
    _ok("data:menu" not in cbs, f"account buttons must be hidden, got {cbs}")


def test_keyboard_without_slots_offers_no_start() -> None:
    _set_proxies(["http://u:p@host:1"])
    _user(UID + 9, 10)
    project = _project()
    cbs = _callbacks(
        tgbot.project_screen_keyboard(project, lang="ru", user_id=UID + 9, ok_count=0)
    )
    _ok(f"queue:ask:{project.id}" not in cbs, "no start button below a full batch")
    _ok("data:menu" not in cbs, "and still no upload prompt")
    _ok("projects:back" in cbs, "back button always present")


def test_classic_keyboard_unchanged() -> None:
    """Regression: classic projects keep the one-tap start and data buttons."""
    _set_proxies(["http://u:p@host:1"])
    uid = UID + 10
    _user(uid, 100, accounts=2)
    project = _classic()
    cbs = _callbacks(
        tgbot.project_screen_keyboard(project, lang="ru", user_id=uid, ok_count=0)
    )
    _ok(f"queue:start:{project.id}" in cbs, f"classic start is one tap, got {cbs}")
    _ok("queue:ask:" not in " ".join(cbs), "no confirm step for classic projects")
    _ok("data:menu" in cbs, "classic projects keep the accounts button")


# ------------------------------------------------------------ confirm screen

def test_confirm_view_starts_and_goes_back() -> None:
    _set_proxies(["http://u:p@host:1"])
    uid = UID + 11
    _user(uid, 100)
    project = _project()
    view = tgbot._project_confirm_view(project, 0, uid, "ru")
    cbs = _callbacks(view.markup)
    _ok(f"queue:start:{project.id}" in cbs, f"confirm must start the job, got {cbs}")
    _ok(f"project:{project.id}" in cbs, "and offer a way back")
    _ok(str(BATCH) in view.text, "restates how many wallets")


def test_confirm_view_warns_about_the_keys() -> None:
    """The file is the only copy the user will ever get — say so before starting."""
    _set_proxies(["http://u:p@host:1"])
    uid = UID + 12
    _user(uid, 100)
    text = tgbot._project_confirm_view(_project(), 0, uid, "ru").text.lower()
    _ok("ключ" in text, f"must mention the keys:\n{text}")
    _ok("второй раз" in text or "не сможет" in text, "must say it cannot be repeated")


def test_confirm_render_routes_through_project_state() -> None:
    """render_project with confirm=1 shows the confirmation, not the normal screen."""
    _set_proxies(["http://u:p@host:1"])
    uid = UID + 13
    _user(uid, 100)
    from bot import registry

    project = _project()
    real = registry.get_project
    tgbot_real = tgbot.get_project
    registry.get_project = lambda _pid, **_kw: project  # type: ignore[assignment]
    tgbot.get_project = lambda _pid, **_kw: project  # type: ignore[assignment]
    try:
        plain = tgbot.render_project(uid, "ru", {"pid": project.id})
        confirm = tgbot.render_project(uid, "ru", {"pid": project.id, "confirm": 1})
    finally:
        registry.get_project = real  # type: ignore[assignment]
        tgbot.get_project = tgbot_real  # type: ignore[assignment]

    _ok(f"queue:ask:{project.id}" in _callbacks(plain.markup), "plain screen asks")
    _ok(f"queue:start:{project.id}" in _callbacks(confirm.markup), "confirm starts")
