"""Auto-registration: opt-in audience, once-per-project marker, finish DM."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="auto_reg_test_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "test:token"
os.environ["AUTOREG_STATE_DIR"] = str(_TMP / "state")
os.environ["USER_DATA_DIR"] = str(_TMP / "ud")
os.environ["USER_PROXY_DIR"] = str(_TMP / "up")

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot."):
        del sys.modules[_mod]

from bot import auto_reg  # noqa: E402
from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import user_data as user_data_mod  # noqa: E402

config.DB_PATH = _TMP / "t.db"
bot_db.DB_PATH = _TMP / "t.db"


def _assert(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class _P:
    id = "newproj"
    name = "New Project"
    requires_captcha = False
    only_user_ids: tuple = ()
    min_ok_limit = 0


OPT_IN = 810001
OPT_IN_NODATA = 810002
OPT_OUT = 810003


def _seed() -> None:
    bot_db.init_db()
    for uid in (OPT_IN, OPT_IN_NODATA, OPT_OUT):
        bot_db.ensure_user(uid, "u", "U")
    bot_db.set_auto_reg(OPT_IN, True)
    bot_db.set_auto_reg(OPT_IN_NODATA, True)
    (Path(config.USER_DATA_DIR)).mkdir(parents=True, exist_ok=True)
    (Path(config.USER_DATA_DIR) / f"{OPT_IN}.txt").write_text(
        "h1|0x" + "aa" * 20 + "\nh2|0x" + "bb" * 20 + "\n", encoding="utf-8"
    )


def test_toggle_and_audience() -> None:
    _seed()
    _assert(bot_db.get_auto_reg(OPT_IN), "on")
    _assert(not bot_db.get_auto_reg(OPT_OUT), "off by default")
    ids = set(bot_db.auto_reg_user_ids())
    _assert(OPT_IN in ids and OPT_IN_NODATA in ids, "both opted in listed")
    _assert(OPT_OUT not in ids, "opted out not listed")
    bot_db.set_auto_reg(OPT_IN_NODATA, False)
    _assert(OPT_IN_NODATA not in bot_db.auto_reg_user_ids(), "toggle off works")


def test_run_queues_only_eligible_and_marks_once() -> None:
    _seed()
    started: list[int] = []

    def _start_job(*, user_id, chat_id, project):
        started.append(user_id)
        job = type("J", (), {"auto_reg": False, "user_id": user_id,
                             "project_id": project.id, "project_name": project.name})
        return True, "", job

    # user_data resolves its dir from os.getenv at call time, and a later
    # collected test module may have repointed the env — so pin the predicate
    # on the exact module object auto_reg uses.
    _orig_gp, _orig_hd = auto_reg.get_project, auto_reg.user_data_mod.has_user_data
    auto_reg.get_project = lambda pid, **kw: _P()  # type: ignore[assignment]
    auto_reg.user_data_mod.has_user_data = (  # type: ignore[assignment]
        lambda uid: uid == OPT_IN
    )
    try:
        s1 = auto_reg.run_for_new_project("newproj", _start_job)
        _assert(s1["reason"] == "ran", s1)
        _assert(started == [OPT_IN], f"only the opted-in user with /data: {started}")
        s2 = auto_reg.run_for_new_project("newproj", _start_job)
        _assert(s2["reason"] == "already_ran", s2)
        _assert(started == [OPT_IN], "marker stops a second sweep")
        s3 = auto_reg.run_for_new_project("newproj", _start_job, force=True)
        _assert(s3["reason"] == "ran", s3)
        _assert(started == [OPT_IN, OPT_IN], "force re-walks without doubling the marker skip")
    finally:
        auto_reg.get_project = _orig_gp  # type: ignore[assignment]
        auto_reg.user_data_mod.has_user_data = _orig_hd  # type: ignore[assignment]


def test_disabled_switch_is_hard() -> None:
    from unittest import mock

    _seed()
    bot_db.set_alert_marker("autoreg_done:offproj", "2026-01-01T00:00:00+00:00")
    with mock.patch.object(auto_reg.config, "AUTO_REG_ENABLED", "0"):
        s = auto_reg.run_for_new_project("offproj", lambda **kw: (True, "", None))
    _assert(s["reason"] == "disabled", s)


def test_finish_message_counts() -> None:
    class J:
        project_id = "newproj"
        project_name = "New Project"
        ok, already, fail, bad = 480, 20, 40, 20
        done, total = 560, 560

    text = auto_reg.finish_message(J(), lang="ru")
    _assert("500" in text, f"ok+already counted: {text}")
    _assert("560" in text, text)
    _assert("New Project" in text, text)
    en = auto_reg.finish_message(J(), lang="en")
    _assert("500" in en and "560" in en, en)


def test_finish_message_after_resume_never_exceeds_total() -> None:
    class J:
        project_id = "newproj"
        project_name = "New Project"
        # A lease resume re-ran deduped rows: counters double-count them.
        ok, already, fail, bad = 681, 304, 0, 0
        done, total = 985, 685

    text = auto_reg.finish_message(J(), lang="ru")
    _assert("685" in text, text)
    _assert("985" not in text, f"resume double-count leaked: {text}")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback

                traceback.print_exc()
                print(f"FAIL {name}: {exc}")
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
