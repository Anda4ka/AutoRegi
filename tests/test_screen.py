"""Anchored live screen: one editable message per user."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="screen_test_"))
_DB = _TMP / "t.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["LIMITS_CACHE_TTL"] = "0"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import screen  # noqa: E402
import bot.db as dbmod  # noqa: E402

config.DB_PATH = _DB
dbmod.DB_PATH = _DB
bot_db.init_db()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class ApiError(Exception):
    """Stand-in for telebot's ApiTelegramException (matched by .description)."""

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 500
        self.edit_error: Exception | None = None
        self.send_error: Exception | None = None

    def send_message(self, chat_id, text, **kw):
        self.calls.append(("send", {"chat_id": chat_id, "text": text, **kw}))
        if self.send_error is not None:
            err, self.send_error = self.send_error, None
            raise err
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)

    def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(
            ("edit", {"chat_id": chat_id, "message_id": message_id, "text": text, **kw})
        )
        if self.edit_error is not None:
            err, self.edit_error = self.edit_error, None
            raise err
        return SimpleNamespace(message_id=message_id)

    def answer_callback_query(self, call_id, text=None, show_alert=False):
        self.calls.append(
            ("answer", {"id": call_id, "text": text, "alert": show_alert})
        )

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


def _call(message_id: int | None = None, chat_id: int = 42):
    message = (
        SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=chat_id))
        if message_id
        else None
    )
    return SimpleNamespace(id="cb1", message=message)


def _register_probe() -> None:
    screen.register("probe", lambda uid, lang, p: screen.View(
        f"probe {p.get('n', 0)} {lang}", markup=None
    ))
    screen.register("other", lambda uid, lang, p: screen.View("other", markup=None))
    screen.register("boom", lambda uid, lang, p: 1 / 0)


_register_probe()


def test_anchor_stored_on_first_show() -> None:
    uid = 800001
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    mid = screen.show(bot, uid, 42, "probe", {"n": 1})
    _assert(mid is not None, "message id returned")
    _assert(bot.kinds() == ["send"], f"first show sends, got {bot.kinds()}")
    chat_id, message_id = screen.anchor(uid)
    _assert(chat_id == 42 and message_id == mid, "anchor stored")
    state, params = screen.current_state(uid)
    _assert(state == "probe" and params == {"n": 1}, f"state stored: {state} {params}")


def test_second_show_edits_the_same_message() -> None:
    uid = 800002
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    first = screen.show(bot, uid, 42, "probe", {"n": 1})
    second = screen.show(bot, uid, 42, "probe", {"n": 2})
    _assert(first == second, "same message reused")
    _assert(bot.kinds() == ["send", "edit"], f"send then edit, got {bot.kinds()}")
    _assert(bot.calls[-1][1]["message_id"] == first, "edited the anchor")


def test_message_is_not_modified_is_success() -> None:
    uid = 800003
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    mid = screen.show(bot, uid, 42, "probe", {"n": 1})
    bot.edit_error = ApiError(
        "Bad Request: message is not modified: specified new message content..."
    )
    again = screen.show(bot, uid, 42, "probe", {"n": 1})
    _assert(again == mid, "anchor kept")
    _assert(
        bot.kinds() == ["send", "edit"],
        f"identical content must not spawn a message, got {bot.kinds()}",
    )


def test_deleted_anchor_is_replaced() -> None:
    uid = 800004
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    first = screen.show(bot, uid, 42, "probe", {"n": 1})
    bot.edit_error = ApiError("Bad Request: message to edit not found")
    second = screen.show(bot, uid, 42, "probe", {"n": 2})
    _assert(second != first, "new message posted")
    _assert(bot.kinds() == ["send", "edit", "send"], f"got {bot.kinds()}")
    _assert(screen.anchor(uid)[1] == second, "anchor moved to the new message")


def test_stale_anchor_sends_new() -> None:
    uid = 800005
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    first = screen.show(bot, uid, 42, "probe", {"n": 1})
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with dbmod.db() as conn:
        conn.execute(
            "UPDATE users SET screen_updated_at = ? WHERE user_id = ?", (old, uid)
        )
        conn.commit()
    second = screen.show(bot, uid, 42, "probe", {"n": 2})
    _assert(second != first, "too old to edit → new message")
    _assert(bot.kinds() == ["send", "send"], f"no edit attempted, got {bot.kinds()}")


def test_parse_error_falls_back_to_plain_text() -> None:
    uid = 800006
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    screen.show(bot, uid, 42, "probe", {"n": 1})
    bot.edit_error = ApiError("Bad Request: can't parse entities: unmatched tag")
    screen.show(bot, uid, 42, "probe", {"n": 2})
    _assert(bot.kinds() == ["send", "edit", "edit"], f"retried plain, got {bot.kinds()}")
    _assert("parse_mode" not in bot.calls[-1][1], "retry drops HTML parse mode")


def test_toast_is_answered_before_the_edit() -> None:
    uid = 800007
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    screen.show(bot, uid, 42, "probe", {"n": 1})
    screen.show(bot, uid, 42, "probe", {"n": 2}, call=_call(), toast_text="Готово")
    kinds = bot.kinds()
    _assert(kinds.index("answer") < kinds.index("edit"), f"answer first: {kinds}")
    answer = [c for k, c in bot.calls if k == "answer"][0]
    _assert(answer["text"] == "Готово", "toast text passed through")


def test_tapped_message_becomes_the_anchor() -> None:
    uid = 800008
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    screen.show(bot, uid, 42, "probe", {"n": 1})
    # User scrolled up and tapped a button on an older bot message.
    screen.show(bot, uid, 42, "probe", {"n": 2}, call=_call(message_id=123))
    _assert(bot.calls[-1][1]["message_id"] == 123, "redraws the tapped message")
    _assert(screen.anchor(uid)[1] == 123, "anchor follows the user")


def test_force_new_reanchors() -> None:
    uid = 800009
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    first = screen.show(bot, uid, 42, "probe", {"n": 1})
    second = screen.show(bot, uid, 42, "probe", {"n": 2}, force_new=True)
    _assert(second != first, "force_new posts a fresh message")
    _assert(bot.kinds() == ["send", "send"], f"no edit, got {bot.kinds()}")


def test_owns_anchor_and_refresh() -> None:
    uid = 800010
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    screen.show(bot, uid, 42, "probe", {"n": 1})
    _assert(screen.owns_anchor(uid, "probe"), "owns probe")
    _assert(not screen.owns_anchor(uid, "other"), "does not own other")
    _assert(screen.refresh(bot, uid), "refresh redraws current state")
    _assert(bot.kinds() == ["send", "edit"], f"refresh edits, got {bot.kinds()}")
    screen.show(bot, uid, 42, "other")
    _assert(screen.owns_anchor(uid, "other"), "state switched")


def test_unknown_state_and_broken_renderer_are_survivable() -> None:
    uid = 800011
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    _assert(screen.show(bot, uid, 42, "no_such_state") is None, "unknown state → None")
    _assert(screen.show(bot, uid, 42, "boom") is None, "renderer crash → None")
    _assert(bot.calls == [], "nothing sent when there is nothing to draw")


def test_user_lock_serialises_writers() -> None:
    uid = 800012
    bot_db.ensure_user(uid, "u", "U")
    order: list[str] = []
    lock = screen.user_lock(uid)

    def worker(tag: str) -> None:
        with screen.user_lock(uid):
            order.append(f"{tag}-in")
            time.sleep(0.05)
            order.append(f"{tag}-out")

    _assert(screen.user_lock(uid) is lock, "same lock per user")
    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    _assert(
        order[0].endswith("-in") and order[1] == order[0].replace("-in", "-out"),
        f"writers did not interleave: {order}",
    )


def test_anchor_survives_a_restart() -> None:
    uid = 800013
    bot_db.ensure_user(uid, "u", "U")
    bot = FakeBot()
    mid = screen.show(bot, uid, 42, "probe", {"n": 7})
    # Simulate a process restart: only the DB carries the anchor.
    chat_id, message_id = screen.anchor(uid)
    state, params = screen.current_state(uid)
    _assert((chat_id, message_id) == (42, mid), "anchor read back from SQLite")
    _assert(state == "probe" and params == {"n": 7}, "state read back from SQLite")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
