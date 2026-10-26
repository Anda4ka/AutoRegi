"""Shared proxy pool kill switch: empty get_proxies, banner, start hidden."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="shared_off_test_"))
_DB = _TMP / "test.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["JOB_PAYLOAD_DIR"] = str(_TMP / "job_payloads")
os.environ.pop("SHARED_PROXIES", None)

for _mod in list(sys.modules):
    if (
        _mod == "bot"
        or _mod.startswith("bot.")
        or _mod == "tgbot"
        or _mod == "handlers"
        or _mod.startswith("handlers.")
    ):
        del sys.modules[_mod]

from bot import alerts  # noqa: E402
from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import proxy_pool  # noqa: E402
from bot import submit_queue as sq  # noqa: E402
from bot import user_data as ud  # noqa: E402
from bot.i18n import t  # noqa: E402

config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]

from bot.registry import get_project, load_projects  # noqa: E402
import tgbot  # noqa: E402

bot_db.init_db()
sq.init_schema()
load_projects()
proxy_pool._proxies_loaded = True
proxy_pool._proxies[:] = ["http://u:p@1.2.3.4:8080"]
proxy_pool._enabled_cache = True


def _restore() -> None:
    os.environ.pop("SHARED_PROXIES", None)
    proxy_pool._enabled_cache = True
    proxy_pool._proxies[:] = ["http://u:p@1.2.3.4:8080"]
    proxy_pool._proxies_loaded = True


def _assert(cond: bool, msg: object = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def test_get_proxies_empty_when_sqlite_off() -> None:
    try:
        proxy_pool.set_shared_enabled(False, admin_id=1)
        _assert(not proxy_pool.shared_enabled())
        _assert(proxy_pool.get_proxies() == [])
        _assert(proxy_pool.loaded_count() == 1)
    finally:
        proxy_pool.set_shared_enabled(True, admin_id=1)
        _restore()


def test_env_hard_off_beats_sqlite_on() -> None:
    try:
        proxy_pool.set_shared_enabled(True, admin_id=1)
        os.environ["SHARED_PROXIES"] = "0"
        proxy_pool._enabled_cache = None
        _assert(not proxy_pool.shared_enabled())
        _assert(proxy_pool.get_proxies() == [])
    finally:
        _restore()


def test_welcome_and_project_show_banner_and_hide_start() -> None:
    uid = 88001
    bot_db.ensure_user(uid, "u", "U")
    ud.save_user_data_from_bytes(
        uid, b"alice|0x1111111111111111111111111111111111111111\n"
    )
    proj = get_project("example")
    try:
        proxy_pool.set_shared_enabled(False, admin_id=1)
        ru = t("ops.own_proxy_only", "ru")
        welcome = tgbot.welcome_text("Ann", load_projects(), user_id=uid, lang="ru")
        _assert(ru in welcome, welcome)
        txt = tgbot.project_ready_text(proj, 0, uid, lang="ru")
        _assert(ru in txt, txt)
        _assert("Загрузите /proxy" in txt or "свои прокси" in txt, txt)
        kb = tgbot.project_screen_keyboard(proj, lang="ru", user_id=uid)
        start = [
            (b.callback_data or "")
            for row in kb.keyboard
            for b in row
            if (b.callback_data or "").startswith("queue:start:")
        ]
        _assert(not start, start)
        _assert(
            "shared proxy pool is off" in tgbot.no_proxies_text("en", name="X").lower()
            or "your own proxies" in tgbot.no_proxies_text("en", name="X").lower(),
            tgbot.no_proxies_text("en", name="X"),
        )
    finally:
        proxy_pool.set_shared_enabled(True, admin_id=1)
        _restore()


def test_empty_file_alert_silent_while_pool_off() -> None:
    """Intentional off is not «proxy.txt пуст» — that page used to fire hourly."""
    if not config.ADMIN_IDS:
        config.ADMIN_IDS.add(1)
    bot_db.set_alert_marker("shared_empty", "")
    orig_gate = alerts._close_projects_whose_gate_shut
    sent: list[str] = []
    alerts._close_projects_whose_gate_shut = lambda send: []
    try:
        os.environ["SHARED_PROXIES"] = "0"
        proxy_pool._enabled_cache = None
        _assert(not alerts._should_alert_shared_empty(0))
        keys = alerts.run_alert_checks(
            shared_proxy_count=0,
            send=lambda _aid, text: sent.append(text),
        )
        _assert("shared_empty" not in keys, keys)
        _assert(not any("proxy.txt пуст" in t for t in sent), sent)
    finally:
        alerts._close_projects_whose_gate_shut = orig_gate
        _restore()


def test_empty_file_alert_fires_when_pool_on() -> None:
    if not config.ADMIN_IDS:
        config.ADMIN_IDS.add(1)
    bot_db.set_alert_marker("shared_empty", "")
    orig_gate = alerts._close_projects_whose_gate_shut
    sent: list[str] = []
    alerts._close_projects_whose_gate_shut = lambda send: []
    try:
        os.environ["SHARED_PROXIES"] = "1"
        proxy_pool.set_shared_enabled(True, admin_id=1)
        proxy_pool._enabled_cache = True
        _assert(alerts._should_alert_shared_empty(0))
        _assert(not alerts._should_alert_shared_empty(3))
        keys = alerts.run_alert_checks(
            shared_proxy_count=0,
            send=lambda _aid, text: sent.append(text),
        )
        _assert("shared_empty" in keys, keys)
        _assert(any("proxy.txt пуст" in t for t in sent), sent)
    finally:
        alerts._close_projects_whose_gate_shut = orig_gate
        _restore()


def test_banner_gone_when_pool_on() -> None:
    uid = 88002
    bot_db.ensure_user(uid, "u", "U")
    try:
        proxy_pool.set_shared_enabled(True, admin_id=1)
        welcome = tgbot.welcome_text("Ann", load_projects(), user_id=uid, lang="ru")
        _assert(t("ops.own_proxy_only", "ru") not in welcome, welcome)
        _assert(proxy_pool.get_proxies() == ["http://u:p@1.2.3.4:8080"])
    finally:
        _restore()
