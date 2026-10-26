"""The mini app's write path: who may change things, and what stops them.

The read-only prototype had one security boundary — `initData` — and
`tests/test_webapp.py` covers it. Letting the page *start work* adds three
more, and each of them is a way to burn the operator's slots or somebody
else's:

* **capability** — a server without an `Actions` object must refuse every
  write rather than half-perform one. A job enqueued where no worker is
  looking is worse than a refusal, because it looks like success.
* **authorisation** — `may_write` is the staged rollout. Until it opens, a
  perfectly valid signature from a stranger still changes nothing.
* **budget** — starting a job spends proxy requests downstream. Its window is
  separate from the polling one, or a chatty screen would spend the protection
  that stands in front of the queue.

The tests drive the real HTTP server with a fake `Actions`, because routing,
verification and the limiter are what a request actually passes through, and
testing the handler functions directly would skip exactly the parts that have
been wrong before.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "123456:AAtest-token-value")
_TMP = Path(tempfile.mkdtemp(prefix="webact_"))
os.environ.setdefault("BOT_DB", str(_TMP / "t.db"))

from bot import config  # noqa: E402
from webapp import actions as webapp_actions  # noqa: E402
from webapp import ratelimit  # noqa: E402
from webapp import server as webapp_server  # noqa: E402

TOKEN = "123456:AAtest-token-value"


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def sign(user_id: int = 7, token: str = TOKEN) -> str:
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAEtest",
        "user": json.dumps({"id": user_id, "first_name": "Op"}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


# ------------------------------------------------------------------- fixtures


class FakeActions(webapp_actions.Actions):
    """A capable Actions that records instead of touching a queue."""

    writable = True
    read_only_reason = "не для вас"

    def __init__(self, allowed: set[int] | None = None) -> None:
        self.allowed = allowed if allowed is not None else {7}
        self.calls: list[tuple] = []
        self.refuse_with: webapp_actions.Denied | None = None
        # None = everyone gets in. Set it to make `gate` refuse, the way a ban
        # or a missing channel subscription does in the real BotActions.
        self.gate_with: webapp_actions.Denied | None = None
        self.ensured: list[int] = []

    def may_write(self, user_id: int) -> bool:
        return int(user_id) in self.allowed

    def gate(self, user_id: int, lang: str = "ru") -> webapp_actions.Denied | None:
        return self.gate_with

    def ensure_user(self, user_id: int, username: str = "",
                    first_name: str = "") -> None:
        self.ensured.append(int(user_id))

    def projects(self, user_id: int, lang: str = "ru") -> list[dict]:
        return [{"id": "demoa", "name": "Demoa", "can_start": True}]

    def caps(self, user_id: int, lang: str = "ru",
             projects: list[dict] | None = None) -> dict:
        return {"tier": "vip", "can_start_all": True,
                "eligible": len(projects or []) or 3}

    def _record(self, name: str, *args):
        if self.refuse_with is not None:
            raise self.refuse_with
        self.calls.append((name, *args))
        return {"ok": True, "message": f"{name} done"}

    def start(self, user_id: int, project_id: str, lang: str = "ru") -> dict:
        return self._record("start", user_id, project_id)

    def start_all(self, user_id: int, lang: str = "ru") -> dict:
        return self._record("start_all", user_id)

    def cancel(self, user_id: int, job_id: int, lang: str = "ru") -> dict:
        return self._record("cancel", user_id, job_id)

    def data(self, user_id: int, lang: str = "ru") -> dict:
        return {"count": 695, "max_lines": 1000, "max_bytes": 512 * 1024}

    def proxies(self, user_id: int, lang: str = "ru") -> dict:
        return {"count": 600, "source": "own", "may_upload": True, "own_file": True}

    def save_data(self, user_id: int, text: str, lang: str = "ru") -> dict:
        return self._record("save_data", user_id, text)

    def clear_data(self, user_id: int, lang: str = "ru") -> dict:
        return self._record("clear_data", user_id)

    def save_proxies(self, user_id: int, text: str, lang: str = "ru") -> dict:
        return self._record("save_proxies", user_id, text)

    def clear_proxies(self, user_id: int, lang: str = "ru") -> dict:
        return self._record("clear_proxies", user_id)

    def billing(self, user_id: int, lang: str = "ru") -> dict:
        return {"enabled": True, "sales_open": True, "tier": "free", "active": None,
                "plans": [{"id": "basic", "price_usd": "50", "days": 30,
                           "ok_limit": 100, "purchasable": True}],
                "open_payment": None}

    def report(self, user_id: int, lang: str = "ru") -> dict:
        return {"has": True, "errors": ["стр. 1: плохой кошелёк"], "errors_total": 1}

    def set_lang(self, user_id: int, lang_code: str) -> dict:
        return self._record("set_lang", user_id, lang_code)

    def buy(self, user_id: int, plan_id: str, lang: str = "ru") -> dict:
        return self._record("buy", user_id, plan_id)

    def check_payment(self, user_id: int, payment_id: str, lang: str = "ru") -> dict:
        return self._record("check_payment", user_id, payment_id)

    def cancel_payment(self, user_id: int, payment_id: str, lang: str = "ru") -> dict:
        return self._record("cancel_payment", user_id, payment_id)

    def deliver(self, user_id: int, project_id: str, lang: str = "ru") -> dict:
        return self._record("deliver", user_id, project_id)

    def suggest(self, user_id: int, text: str, lang: str = "ru") -> dict:
        return self._record("suggest", user_id, text)


def _seed_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                            lang TEXT, banned INTEGER);
        CREATE TABLE submit_jobs (id INTEGER PRIMARY KEY, user_id INTEGER, project_id TEXT,
                                  status TEXT, total INTEGER, done INTEGER, ok INTEGER,
                                  fail INTEGER, already INTEGER, bad INTEGER, chat_id INTEGER,
                                  message_id INTEGER, detail TEXT, created_at TEXT,
                                  started_at TEXT, finished_at TEXT);
        CREATE TABLE user_project_stats (user_id INTEGER, project_id TEXT, ok_count INTEGER,
                                         updated_at TEXT);
        """
    )
    conn.execute("INSERT INTO users VALUES (7, 'op', 'Op', 'ru', 0)")
    conn.commit()
    conn.close()


_DB = str(_TMP / "state.db")
_seed_db(_DB)
_SERVER = None
_PORT = 0


def _start_server():
    global _SERVER, _PORT
    if _SERVER is not None:
        return
    config.BOT_TOKEN = TOKEN
    config.DB_PATH = _DB
    _SERVER = webapp_server.build_server("127.0.0.1", 0)
    _PORT = _SERVER.server_address[1]
    threading.Thread(target=_SERVER.serve_forever, daemon=True).start()


def post(path: str, body: dict, *, raw_init: str | None = None) -> tuple[int, dict]:
    _start_server()
    payload = dict(body)
    payload["initData"] = raw_init if raw_init is not None else sign()
    req = urllib.request.Request(
        f"http://127.0.0.1:{_PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "CF-Connecting-IP": "203.0.113.5"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body_text = exc.read() or b"{}"
        try:
            return exc.code, json.loads(body_text)
        except ValueError:
            return exc.code, {}


def upload(path: str, body: bytes, *, init: str | None = None,
           header: bool = True) -> tuple[int, dict]:
    """POST a file the way the page does: raw body, credential in a header."""
    _start_server()
    headers = {"Content-Type": "text/plain; charset=utf-8",
               "CF-Connecting-IP": "203.0.113.5"}
    if header:
        headers["X-Init-Data"] = init if init is not None else sign()
    req = urllib.request.Request(
        f"http://127.0.0.1:{_PORT}{path}", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def use(actions: webapp_actions.Actions) -> None:
    webapp_server.ACTIONS = actions
    ratelimit.by_ip.reset()
    ratelimit.by_user.reset()
    ratelimit.by_write.reset()


# -------------------------------------------------------------- capability


def test_the_base_actions_refuse_every_write() -> None:
    """Refusal is the default, so nothing becomes possible by forgetting."""
    base = webapp_actions.Actions()
    _ok(base.writable is False)
    for call in (
        lambda: base.start(7, "demoa"),
        lambda: base.start_all(7),
        lambda: base.cancel(7, 1),
    ):
        try:
            call()
            raise AssertionError("a write went through on the read-only base")
        except webapp_actions.Denied as exc:
            _ok(exc.code == "read_only", exc.code)


def test_a_read_only_server_refuses_starts_without_pretending() -> None:
    """No 500 and no silent success: the answer is a sentence the user reads."""
    use(webapp_actions.ReadOnly())
    status, body = post("/api/start", {"project_id": "demoa"})
    _ok(status == 200, status)
    _ok(body["ok"] is False, body)
    _ok(body["code"] == "read_only", body)
    _ok(body["message"], "a refusal must say something")


def test_reading_still_works_when_writing_does_not() -> None:
    use(webapp_actions.ReadOnly())
    status, body = post("/api/state", {})
    _ok(status == 200, status)
    _ok(body["can_write"] is False, body)
    _ok(body["projects"] == [], "the read-only base has no project list to give")


# ----------------------------------------------------------- authorisation


def test_a_valid_signature_from_a_stranger_changes_nothing() -> None:
    """The rollout gate: right bot, real user, still not allowed to start."""
    fake = FakeActions(allowed={7})
    use(fake)
    status, body = post("/api/start", {"project_id": "demoa"}, raw_init=sign(user_id=99))
    _ok(status == 200, status)
    _ok(body["ok"] is False and body["code"] == "read_only", body)
    _ok(fake.calls == [], f"a stranger reached the queue: {fake.calls}")


def test_an_allowed_user_starts_and_gets_the_new_state_back() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    status, body = post("/api/start", {"project_id": "demoa"})
    _ok(status == 200, status)
    _ok(body["ok"] is True, body)
    _ok(fake.calls == [("start", 7, "demoa")], fake.calls)
    # The state travels with the answer so the screen after a tap cannot race
    # a separate refresh and show the moment before it.
    _ok("state" in body and body["state"]["can_write"] is True, body.get("state"))


def test_the_user_id_comes_from_the_signature_not_the_body() -> None:
    """A body field must never be able to act for someone else."""
    fake = FakeActions(allowed={7, 99})
    use(fake)
    post("/api/start", {"project_id": "demoa", "user_id": 99}, raw_init=sign(user_id=7))
    _ok(fake.calls == [("start", 7, "demoa")], fake.calls)


def test_a_forged_signature_never_reaches_the_gate() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    bad = sign(user_id=7, token="999999:someone-elses-bot")
    status, _body = post("/api/start", {"project_id": "demoa"}, raw_init=bad)
    _ok(status == 401, status)
    _ok(fake.calls == [], fake.calls)


# ------------------------------------------------------------------ routing


def test_cancel_and_start_all_reach_the_right_action() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    _ok(post("/api/cancel", {"job_id": 42})[1]["ok"] is True)
    _ok(post("/api/start_all", {})[1]["ok"] is True)
    _ok(fake.calls == [("cancel", 7, 42), ("start_all", 7)], fake.calls)


def test_an_unknown_route_is_not_invented() -> None:
    use(FakeActions())
    _ok(post("/api/delete_everything", {})[0] == 404)


def test_a_junk_job_id_is_a_refusal_not_a_crash() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    status, body = post("/api/cancel", {"job_id": "; DROP TABLE"})
    _ok(status == 200, status)
    _ok(body["ok"] is False and body["code"] == "bad_request", body)
    _ok(fake.calls == [], fake.calls)


def test_a_gate_saying_no_is_not_an_error() -> None:
    """"Нет слотов" is an answer. Rendering it as a 500 teaches users to retry."""
    fake = FakeActions(allowed={7})
    fake.refuse_with = webapp_actions.Denied("refused", "Лимит исчерпан")
    use(fake)
    status, body = post("/api/start", {"project_id": "demoa"})
    _ok(status == 200, status)
    _ok(body["ok"] is False and body["message"] == "Лимит исчерпан", body)
    _ok(body.get("state") is not None, "a refusal should still refresh the screen")


# ------------------------------------------------------------------- budget


def test_starts_have_their_own_budget_and_polling_does_not_spend_it() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    # Polling hard first: if the two shared a window, this would exhaust it.
    for _ in range(ratelimit.WRITE_LIMIT + 3):
        _ok(post("/api/state", {})[0] == 200)
    status, body = post("/api/start", {"project_id": "demoa"})
    _ok(status == 200 and body["ok"] is True, body)

    use(fake)  # fresh windows
    fake.calls.clear()
    codes = [post("/api/start", {"project_id": "demoa"})[0]
             for _ in range(ratelimit.WRITE_LIMIT + 2)]
    _ok(codes.count(429) >= 2, f"the write window never closed: {codes}")
    _ok(len(fake.calls) <= ratelimit.WRITE_LIMIT,
        f"{len(fake.calls)} starts got through a limit of {ratelimit.WRITE_LIMIT}")


# ------------------------------------------------------------------ uploads


def test_an_uploaded_file_arrives_as_the_text_that_was_sent() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    body = "alice|0x1111111111111111111111111111111111111111\n"
    status, res = upload("/api/data", body.encode())
    _ok(status == 200 and res["ok"] is True, res)
    _ok(fake.calls == [("save_data", 7, body)], fake.calls)
    _ok("state" in res, "an upload should hand back the refreshed screen")


def test_an_upload_without_the_header_never_reaches_the_file() -> None:
    """The credential moved to a header; missing it must fail like a bad one."""
    fake = FakeActions(allowed={7})
    use(fake)
    status, _res = upload("/api/data", b"alice|0x1", header=False)
    _ok(status == 401, status)
    _ok(fake.calls == [], fake.calls)


def test_a_stranger_cannot_upload_either() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    status, res = upload("/api/proxies", b"1.2.3.4:8080:u:p", init=sign(user_id=99))
    _ok(status == 200 and res["ok"] is False and res["code"] == "read_only", res)
    _ok(fake.calls == [], fake.calls)


def test_a_file_over_the_cap_is_refused_by_size_not_by_parsing_it() -> None:
    """The point of the cap is to not hold the megabyte in memory at all."""
    fake = FakeActions(allowed={7})
    use(fake)
    oversized = b"x" * (webapp_server._upload_cap() + 1024)
    status, res = upload("/api/data", oversized)
    _ok(status == 200 and res["ok"] is False and res["code"] == "too_big", res)
    _ok("КБ" in res["message"], "the refusal should say how much is too much")
    _ok(fake.calls == [], fake.calls)


def test_an_empty_upload_is_a_bad_request() -> None:
    use(FakeActions(allowed={7}))
    _ok(upload("/api/data", b"")[0] == 400)


def test_a_windows_encoded_file_still_reads() -> None:
    """Exports from a Windows tool are cp1251, and rejecting them is our bug."""
    fake = FakeActions(allowed={7})
    use(fake)
    text = "миша|0x2222222222222222222222222222222222222222\n"
    status, res = upload("/api/data", text.encode("cp1251"))
    _ok(status == 200 and res["ok"] is True, res)
    _ok(fake.calls == [("save_data", 7, text)], fake.calls)


def test_uploads_spend_the_write_budget_not_the_polling_one() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    codes = [upload("/api/data", b"a|0x1")[0] for _ in range(ratelimit.WRITE_LIMIT + 2)]
    _ok(codes.count(429) >= 2, f"uploads ignored the write window: {codes}")


def test_clearing_goes_through_the_same_gate_as_everything_else() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    _ok(post("/api/data/clear", {})[1]["ok"] is True)
    _ok(post("/api/proxies/clear", {})[1]["ok"] is True)
    _ok(fake.calls == [("clear_data", 7), ("clear_proxies", 7)], fake.calls)

    use(fake)
    fake.calls.clear()
    status, res = post("/api/data/clear", {}, raw_init=sign(user_id=99))
    _ok(status == 200 and res["ok"] is False, res)
    _ok(fake.calls == [], "a stranger deleted someone's accounts")


def test_the_state_carries_what_the_setup_cards_need() -> None:
    use(FakeActions(allowed={7}))
    _status, s = post("/api/state", {})
    _ok(s["data"]["count"] == 695, s["data"])
    _ok(s["proxies"]["source"] == "own", s["proxies"])


def test_the_upload_cap_covers_a_full_sized_file() -> None:
    """A cap below the bot's own ceiling would reject files the chat accepts."""
    _ok(webapp_server._upload_cap() > int(config.USER_DATA_MAX_BYTES),
        (webapp_server._upload_cap(), config.USER_DATA_MAX_BYTES))
    _ok(webapp_server._upload_cap() > int(config.USER_PROXY_MAX_BYTES))
    _ok(webapp_server.MAX_BODY < webapp_server._upload_cap(),
        "the small routes must not inherit the upload allowance")


# ------------------------------------------------------- plans and the rest


def test_the_money_routes_stay_behind_the_same_gate() -> None:
    """Payments move real money. They get no exemption from the rollout gate."""
    fake = FakeActions(allowed={7})
    use(fake)
    stranger = sign(user_id=99)
    for path, body in (
        ("/api/buy", {"plan": "pro"}),
        ("/api/pay/check", {"payment_id": "p1"}),
        ("/api/pay/cancel", {"payment_id": "p1"}),
    ):
        status, res = post(path, body, raw_init=stranger)
        _ok(status == 200 and res["ok"] is False and res["code"] == "read_only", (path, res))
    _ok(fake.calls == [], f"a stranger reached the payment code: {fake.calls}")


def test_buying_checking_and_cancelling_reach_the_right_action() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    _ok(post("/api/buy", {"plan": "pro"})[1]["ok"] is True)
    _ok(post("/api/pay/check", {"payment_id": "np-1"})[1]["ok"] is True)
    _ok(post("/api/pay/cancel", {"payment_id": "np-1"})[1]["ok"] is True)
    _ok(fake.calls == [("buy", 7, "pro"), ("check_payment", 7, "np-1"),
                       ("cancel_payment", 7, "np-1")], fake.calls)


def test_a_missing_or_oversized_field_is_refused_before_the_action() -> None:
    """Free-text fields have no natural length; the route gives them one."""
    fake = FakeActions(allowed={7})
    use(fake)
    for path, body in (
        ("/api/buy", {}),
        ("/api/buy", {"plan": "x" * 64}),
        ("/api/suggest", {"text": "  "}),
        ("/api/deliver", {"project_id": ""}),
    ):
        status, res = post(path, body)
        _ok(status == 200 and res["ok"] is False and res["code"] == "bad_request",
            (path, body, res))
    _ok(fake.calls == [], fake.calls)


def test_the_rollout_gate_reads_its_setting_the_way_it_is_documented() -> None:
    """`WEBAPP_WRITE_USERS`: empty = admins, a list = those ids, `*` = everyone.

    Opening the web to everyone grants nothing the chat buttons do not already
    grant — same gates, same queue, same limits — so the flag is about how much
    of the new code is trusted, not about permissions. That makes it the kind
    of setting somebody changes quickly, which is a reason to pin its meaning.
    """
    import tgbot  # noqa: PLC0415 — heavy import, only this test needs it

    actions = tgbot.BotActions(None)
    # Hermetic precondition: "empty = admins" is meaningless without an admin,
    # and a machine without .env (CI) has none. Bring our own instead of
    # borrowing whatever the local environment happens to grant.
    admin = 999001
    stranger = 111222333
    previous_admins = set(tgbot.config.ADMIN_IDS)
    previous = tgbot.config.WEBAPP_WRITE_USERS
    try:
        tgbot.config.ADMIN_IDS = {admin}
        tgbot.config.WEBAPP_WRITE_USERS = ""
        _ok(actions.may_write(admin) is True, "empty must still let an admin in")
        _ok(actions.may_write(stranger) is False, "empty must mean admins only")

        tgbot.config.WEBAPP_WRITE_USERS = "*"
        _ok(actions.may_write(stranger) is True, "* must open it to everyone")

        tgbot.config.WEBAPP_WRITE_USERS = f"{stranger}, 777"
        _ok(actions.may_write(stranger) is True, "a listed id is allowed")
        _ok(actions.may_write(888) is False, "an unlisted id is not")

        tgbot.config.WEBAPP_WRITE_USERS = "мусор"
        _ok(actions.may_write(stranger) is False,
            "an unparseable list must close the door, not open it")
    finally:
        tgbot.config.WEBAPP_WRITE_USERS = previous
        tgbot.config.ADMIN_IDS = previous_admins


def test_language_is_not_a_capability() -> None:
    """It changes what one person reads, so it is not behind the rollout gate."""
    fake = FakeActions(allowed=set())
    use(fake)
    status, res = post("/api/lang", {"lang": "en"})
    _ok(status == 200 and res["ok"] is True, res)
    _ok(fake.calls == [("set_lang", 7, "en")], fake.calls)


def test_deliver_and_suggest_carry_their_arguments() -> None:
    fake = FakeActions(allowed={7})
    use(fake)
    post("/api/deliver", {"project_id": "fleurhood"})
    post("/api/suggest", {"text": "Новый проект https://x.io"})
    _ok(fake.calls == [
        ("deliver", 7, "fleurhood"),
        ("suggest", 7, "Новый проект https://x.io"),
    ], fake.calls)


def test_the_state_carries_the_plan_and_report_blocks() -> None:
    use(FakeActions(allowed={7}))
    _status, s = post("/api/state", {})
    _ok(s["billing"]["enabled"] is True and s["billing"]["plans"], s["billing"])
    _ok(s["report"]["errors_total"] == 1, s["report"])


def test_payment_routes_spend_the_write_budget() -> None:
    """A check hits the payment provider's API; it is not a free tap."""
    fake = FakeActions(allowed={7})
    use(fake)
    codes = [post("/api/pay/check", {"payment_id": "p"})[0]
             for _ in range(ratelimit.WRITE_LIMIT + 2)]
    _ok(codes.count(429) >= 2, f"payment checks ignored the write window: {codes}")


def test_the_write_budget_is_small_enough_to_matter() -> None:
    """Each start costs real proxy requests; the number has to be a number."""
    _ok(ratelimit.WRITE_LIMIT <= 20, ratelimit.WRITE_LIMIT)
    _ok(ratelimit.WRITE_LIMIT < ratelimit.USER_LIMIT,
        "a write budget at or above the poll budget protects nothing")


# -------------------------------------------------------------------- gate
#
# The gate the web did not have. `is_banned` was checked in nine chat handlers
# and in none of the web path, and the channel subscription gate existed only
# as the `subcheck` callback — so a banned account, or one that had never
# joined the required channel, could open the mini app and reach every route
# the chat refuses it. These tests exist so that stays fixed.


_BAN = webapp_actions.Denied("banned", "⛔ Доступ ограничен.")
_NOSUB = webapp_actions.Denied("not_subscribed", "Подпишись на канал.")


def test_a_banned_user_is_refused_before_any_route_runs() -> None:
    fake = FakeActions(allowed={7})
    fake.gate_with = _BAN
    use(fake)
    for path, body in (
        ("/api/state", {}),
        ("/api/start", {"project_id": "demoa"}),
        ("/api/start_all", {}),
        ("/api/cancel", {"job_id": 1}),
        ("/api/buy", {"plan": "basic"}),
        ("/api/lang", {"lang": "en"}),
    ):
        status, answer = post(path, body)
        _ok(status == 403, f"{path} answered {status}, not 403")
        _ok(answer.get("code") == "banned", f"{path}: {answer}")
        _ok(answer.get("message"), f"{path} refused without saying why")
    _ok(fake.calls == [], f"a banned user reached the actions: {fake.calls}")


def test_a_user_who_never_joined_the_channel_is_refused_too() -> None:
    """The gate that pays for the whole product, and the web skipped it."""
    fake = FakeActions(allowed={7})
    fake.gate_with = _NOSUB
    use(fake)
    status, answer = post("/api/start", {"project_id": "demoa"})
    _ok(status == 403, status)
    _ok(answer["code"] == "not_subscribed", answer)
    _ok(fake.calls == [], fake.calls)


def test_uploads_go_through_the_gate_before_reading_the_body() -> None:
    """The route that accepts half a megabyte must refuse first, read second."""
    fake = FakeActions(allowed={7})
    fake.gate_with = _BAN
    use(fake)
    for path in ("/api/data", "/api/proxies"):
        status, answer = upload(path, b"handle|0x" + b"a" * 40)
        _ok(status == 403, f"{path} answered {status}, not 403")
        _ok(answer.get("code") == "banned", f"{path}: {answer}")
    _ok(fake.calls == [], f"a banned user wrote a file: {fake.calls}")


def test_an_open_gate_changes_nothing_for_everyone_else() -> None:
    """The gate must be a filter, not a new refusal for ordinary users."""
    fake = FakeActions(allowed={7})
    use(fake)  # gate_with stays None
    status, body = post("/api/start", {"project_id": "demoa"})
    _ok(status == 200 and body["ok"] is True, body)
    _ok(fake.calls == [("start", 7, "demoa")], fake.calls)


def test_the_rate_limit_runs_before_the_user_row_is_written() -> None:
    """`ensure_user` is a write; a replayed initData must not spend it freely.

    It used to sit above the per-user window, so a captured initData could
    drive one database write per request until the *IP* budget — six times
    larger — ran out. The order is the whole fix.
    """
    fake = FakeActions(allowed={7})
    use(fake)
    limit = ratelimit.USER_LIMIT
    for _ in range(limit + 12):
        post("/api/state", {})
    _ok(len(fake.ensured) <= limit,
        f"{len(fake.ensured)} user writes for a budget of {limit}")


def test_the_gate_is_asked_once_per_request_not_once_per_route() -> None:
    """Cheap enough to sit in front of a four-second poll."""
    asked = []

    class Counting(FakeActions):
        def gate(self, user_id: int, lang: str = "ru"):
            asked.append(int(user_id))
            return None

    use(Counting(allowed={7}))
    post("/api/state", {})
    _ok(asked == [7], asked)


# ------------------------------------------------------------------ flood
#
# The web server runs inside the bot process, so "the site got slow" and "the
# queue stopped" are the same event. These guard the three defaults that were
# inherited rather than chosen.


def test_the_listen_backlog_is_not_the_standard_librarys_five() -> None:
    """Five is the `socketserver` default, and the kernel drops the sixth.

    No 429, no log line — the client just sees a reset. Behind a tunnel that
    multiplexes many requests onto one connection, ordinary traffic reaches it.
    """
    _ok(webapp_server._QuietServer.request_queue_size >= 64,
        f"backlog is {webapp_server._QuietServer.request_queue_size}")


def test_concurrent_handlers_are_capped() -> None:
    """Thread-per-connection with no ceiling, next to the submit queue."""
    _ok(webapp_server.MAX_IN_FLIGHT > 0)
    _ok(webapp_server.MAX_IN_FLIGHT <= 64,
        f"{webapp_server.MAX_IN_FLIGHT} handlers is not a cap")
    _ok(webapp_server._IN_FLIGHT._initial_value == webapp_server.MAX_IN_FLIGHT)


def test_the_page_itself_is_rate_limited() -> None:
    """It was the only unmetered route, and the cheapest one to ask for."""
    use(FakeActions())
    _start_server()
    ratelimit.by_ip.reset()

    def get() -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{_PORT}/",
            headers={"CF-Connecting-IP": "198.51.100.9"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code

    _ok(get() == 200, "the page must still serve")
    seen = {get() for _ in range(ratelimit.IP_LIMIT + 8)}
    _ok(429 in seen, f"a GET flood was never refused: {seen}")
    ratelimit.by_ip.reset()


def test_the_page_is_not_read_from_disk_every_time() -> None:
    """A fixed 48 KB asset, and it was re-read per request."""
    body_a = webapp_server._index_bytes()
    tag_a = webapp_server._index_etag()
    _ok(body_a and tag_a, "the page did not load")
    _ok(webapp_server._index_bytes() is body_a,
        "the cached page was rebuilt instead of reused")
    _ok(webapp_server._index_etag() == tag_a, "the ETag is not stable")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
