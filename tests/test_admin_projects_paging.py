"""The admin Projects screen must be able to reach every project.

It used to build enable-buttons for the runtime-disabled ones and then
`list_projects()[:12]` for the live half. With 23 live projects that hid eleven
of them, and this is the screen whose whole job is switching one off — the
operator hit it on 2026-08-19 trying to turn one off and not finding it.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="admin_paging_"))
os.environ["BOT_DB"] = str(_TMP / "test.db")
os.environ["BOT_TOKEN"] = "test:token"
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402

config.DB_PATH = str(_TMP / "test.db")
bot_db.DB_PATH = str(_TMP / "test.db")  # type: ignore[attr-defined]

import tgbot  # noqa: E402

from bot.registry import list_projects, load_projects  # noqa: E402

load_projects()
bot_db.init_db()


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _off_buttons(kb) -> list[str]:
    out = []
    for row in kb.keyboard:
        for btn in row:
            data = getattr(btn, "callback_data", "") or ""
            if data.startswith("admin:proj_off:"):
                out.append(data.split(":", 2)[2])
    return out


def _nav(kb) -> list[str]:
    out = []
    for row in kb.keyboard:
        for btn in row:
            data = getattr(btn, "callback_data", "") or ""
            if data.startswith("admin:projects:"):
                out.append(data)
    return out


def test_every_live_project_is_reachable_across_the_pages() -> None:
    live = [p.id for p in list_projects()]
    _ok(live, "the registry must have live projects to page through")
    seen: list[str] = []
    page = 0
    while True:
        kb = tgbot.admin_keyboard_projects(page)
        seen.extend(_off_buttons(kb))
        if f"admin:projects:{page + 1}" not in _nav(kb):
            break
        page += 1
        _ok(page < 50, "the pager must terminate")
    _ok(sorted(seen) == sorted(live), (sorted(set(live) - set(seen)), len(seen)))


def test_a_page_holds_at_most_the_page_size() -> None:
    kb = tgbot.admin_keyboard_projects(0)
    _ok(len(_off_buttons(kb)) <= tgbot.ADMIN_PROJECTS_PER_PAGE, _off_buttons(kb))


def test_an_out_of_range_page_lands_on_the_last_one_not_on_an_empty_screen() -> None:
    last = tgbot.admin_keyboard_projects(999)
    _ok(_off_buttons(last), "a bad page number must still show projects")


def test_the_enable_buttons_are_never_paged_away() -> None:
    """After an alert the admin comes here for exactly this button."""
    pid = list_projects()[0].id
    bot_db.set_project_runtime_disabled(pid, True, admin_id=1)
    try:
        for page in range(3):
            kb = tgbot.admin_keyboard_projects(page)
            on = [
                (getattr(b, "callback_data", "") or "")
                for row in kb.keyboard
                for b in row
                if (getattr(b, "callback_data", "") or "").startswith("admin:proj_on:")
            ]
            _ok(f"admin:proj_on:{pid}" in on, (page, on))
    finally:
        bot_db.set_project_runtime_disabled(pid, False, admin_id=1)
