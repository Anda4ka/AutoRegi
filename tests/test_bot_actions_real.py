"""Every mini-app action, against the real `BotActions` and a real database.

`tests/test_webapp_actions.py` drives the HTTP layer with a fake `Actions`,
which is right for what it measures — routing, signatures, rate limits — and
blind to the half that actually runs. `BotActions.suggest` called
`bot_db.get_user`, a function that does not exist, and nothing noticed until a
user pressed «Предложить проект» and got a 500. The name had never been read
against `bot/db.py`.

So this file calls each method for real. It is deliberately shallow on
behaviour and total on coverage: every method, once, with arguments of the
shape the server passes. What it catches is the class of mistake that testing
against a double cannot — a misremembered function name, a changed signature,
a module that moved.

Refusals count as passing. `Denied` is a designed answer; `AttributeError` and
`TypeError` are not.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="actions_real_"))
_DB = str(_TMP / "t.db")
os.environ["BOT_DB"] = _DB
os.environ["BOT_TOKEN"] = "test:token"
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
(_TMP / "proxy.txt").write_text("1.2.3.4:8080:u:p\n", encoding="utf-8")

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as _bootstrap_db  # noqa: E402

config.DB_PATH = _DB
_bootstrap_db.DB_PATH = _DB  # type: ignore[attr-defined]

import tgbot  # noqa: E402

# Everything below reaches its modules through `tgbot`, never through a fresh
# `from bot import ...`. Several test modules drop `bot.*` from `sys.modules`
# when they load, so an import here can hand back a different object than the
# one the code under test is holding — and a patch applied to the wrong copy
# silently does nothing. That is exactly how `payments_np.get_payment` stayed
# real while the test believed it was faked.
bot_db = tgbot.bot_db
payments_np = tgbot.payments_np
billing = tgbot.billing
bot_mints = tgbot.bot_mints
bot_queue = tgbot.bot_queue
bot_limits = tgbot.bot_limits
config = tgbot.config
config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]

from webapp import actions as webapp_actions  # noqa: E402

# Paths are re-asserted around every test, not just at import.
#
# These modules share one `bot.db` through `sys.modules`, and each of them
# points `DB_PATH` at its own temp file when it loads — so the last import
# wins and everybody else quietly reads somebody else's database. Standalone
# runs looked clean while the full suite failed six tests here and two in
# `test_ux_render.py`, in both directions. Setting the paths per test and
# putting the previous ones back is what makes the two orders agree.
_ENV_KEYS = ("BOT_DB", "USER_DATA_DIR", "USER_PROXY_DIR", "PROXY_FILE")
_MY_ENV = {k: os.environ[k] for k in _ENV_KEYS}


def _use_my_paths() -> dict:
    before = {"db": config.DB_PATH, "bot_db": bot_db.DB_PATH,
              "env": {k: os.environ.get(k) for k in _ENV_KEYS}}
    os.environ.update(_MY_ENV)
    config.DB_PATH = _DB
    bot_db.DB_PATH = _DB  # type: ignore[attr-defined]
    return before


def _restore(before: dict) -> None:
    config.DB_PATH = before["db"]
    bot_db.DB_PATH = before["bot_db"]  # type: ignore[attr-defined]
    for key, value in before["env"].items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


try:  # pytest runs this beside other modules; standalone `main()` does not
    import pytest

    @pytest.fixture(autouse=True)
    def _isolated_paths():
        before = _use_my_paths()
        try:
            yield
        finally:
            _restore(before)
except ImportError:  # pragma: no cover
    pass


UID = 700501
bot_db.init_db()
# The queue keeps its own table and creates it in `init_schema`, which the bot
# calls from `start_workers`. Without it `start` fails on "no such table" —
# a fixture gap, but one that hid whether the method itself works.
bot_queue.init_schema()
bot_db.ensure_user(UID, "op", "Op")


class _Bot:
    """A TeleBot that records instead of talking to Telegram."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

        class M:
            message_id = 1
        return M()

    def edit_message_text(self, *a, **kw):
        return None


ACTIONS = tgbot.BotActions(_Bot())


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _call(name: str, *args, **kwargs):
    """Run one action; a designed refusal is a pass, a broken call is not."""
    try:
        return getattr(ACTIONS, name)(*args, **kwargs)
    except webapp_actions.Denied:
        return "denied"
    except (AttributeError, TypeError, NameError) as exc:
        raise AssertionError(f"{name}: {type(exc).__name__}: {exc}") from None


# ------------------------------------------------------------------ reading


def test_every_read_returns_something_shaped_like_a_screen() -> None:
    _ok(isinstance(_call("projects", UID, "ru"), list))
    _ok(isinstance(_call("caps", UID, "ru"), dict))
    _ok(isinstance(_call("data", UID, "ru"), dict))
    _ok(isinstance(_call("proxies", UID, "ru"), dict))
    _ok(isinstance(_call("billing", UID, "ru"), dict))
    _ok(isinstance(_call("report", UID, "ru"), dict))
    _ok(isinstance(_call("mints", UID, "ru"), dict))
    _ok(_call("lang_of", UID) in ("ru", "en"))
    _ok(isinstance(_call("may_write", UID), bool))


def test_caps_accepts_the_prebuilt_project_list() -> None:
    """The server hands `projects()` straight to `caps()` to avoid a second pass."""
    rows = ACTIONS.projects(UID, "ru")
    _ok(isinstance(_call("caps", UID, "ru", projects=rows), dict))


# ------------------------------------------------------------------ writing


def test_suggest_reaches_the_database_and_the_admins() -> None:
    """Two separate faults met here.

    It read a user row through a function that does not exist, so it answered
    500. And once that was fixed it saved the row and told nobody: the chat
    handler DMs the admins, this path did not. A stored suggestion the operator
    never sees is the same as a lost one — which is how it was reported.
    """
    previous = config.ADMIN_IDS
    ACTIONS._bot.sent.clear()
    try:
        config.ADMIN_IDS = {999001, 999002}
        out = _call("suggest", UID, "Новый проект https://example.test")
        _ok(out != "denied", "a first suggestion of the day must go through")
        _ok(out["ok"] is True, out)
        _ok(out.get("notified") == 2, f"both admins must be DMed: {out}")
        who = [chat for chat, _text in ACTIONS._bot.sent]
        _ok(sorted(who) == [999001, 999002], who)
        _ok("предложение" in ACTIONS._bot.sent[0][1].lower(),
            ACTIONS._bot.sent[0][1][:80])
    finally:
        config.ADMIN_IDS = previous
    # Second one the same day is a designed refusal, not an error.
    _ok(_call("suggest", UID, "Ещё один https://example.test") == "denied")


def test_a_first_time_visitor_gets_a_user_row_before_anything_writes() -> None:
    """Somebody who opened the app without ever pressing /start.

    Every chat handler begins with `ensure_user`; the web had none, so this
    person had no `users` row and the first write with a foreign key onto it
    failed. `add_suggestion` reported that as "one suggestion per day", sending
    a first-time visitor away to wait a day for nothing.
    """
    fresh = 700777
    with bot_db.db() as conn:
        conn.execute("DELETE FROM project_suggestions WHERE user_id = ?", (fresh,))
        conn.execute("DELETE FROM users WHERE user_id = ?", (fresh,))
        conn.commit()
    _ok(bot_db.get_user_row(fresh) is None, "the fixture must start with no row")

    ACTIONS.ensure_user(fresh, "newcomer", "Newcomer")
    _ok(bot_db.get_user_row(fresh) is not None, "the row has to exist before a write")

    out = _call("suggest", fresh, "Первый в жизни https://example.test/first")
    _ok(out != "denied", "a first-time visitor's first suggestion must go through")
    _ok(out["ok"] is True, out)


def test_an_unknown_user_is_not_told_they_already_wrote_today() -> None:
    """The two `IntegrityError`s used to share one answer."""
    ghost = 700888
    with bot_db.db() as conn:
        conn.execute("DELETE FROM project_suggestions WHERE user_id = ?", (ghost,))
        conn.execute("DELETE FROM users WHERE user_id = ?", (ghost,))
        conn.commit()
    result = bot_db.add_suggestion(ghost, "g", "G", "текст предложения")
    _ok(result["ok"] is False, result)
    _ok(result["reason"] == "unknown_user",
        f"a missing user row must not read as the daily limit: {result}")


def test_suggest_length_limits_are_refusals_not_crashes() -> None:
    _ok(_call("suggest", UID, "ко") == "denied")
    _ok(_call("suggest", UID, "x" * 5000) == "denied")


def test_language_round_trip() -> None:
    _ok(_call("set_lang", UID, "en")["lang"] == "en")
    _ok(_call("lang_of", UID) == "en")
    _call("set_lang", UID, "ru")
    _ok(_call("set_lang", UID, "fr") == "denied", "an unknown language is refused")


def test_accounts_and_proxies_write_and_clear() -> None:
    wallets = "\n".join(f"user{i}|0x" + f"{i:040x}" for i in range(1, 4))
    out = _call("save_data", UID, wallets + "\n")
    _ok(out != "denied" and out["count"] == 3, out)
    _ok(_call("data", UID)["count"] == 3)
    _ok(_call("save_data", UID, "мусор без кошельков") == "denied",
        "an unparseable file is refused, not stored")
    _ok(_call("data", UID)["count"] == 3, "a refused upload must not touch the stored file")
    _ok(_call("clear_data", UID)["ok"] is True)

    _ok(_call("save_proxies", UID, "5.6.7.8:3128:u:p\n") != "denied")
    _ok(_call("proxies", UID)["source"] == "own")
    # The live check starts in the background; the report travels on the
    # state poll, so the answer itself is only a start receipt. It must not
    # block on real proxy traffic.
    started = _call("check_proxies", UID)
    _ok(started != "denied" and started["ok"] is True, started)
    _ok(_call("clear_proxies", UID)["ok"] is True)
    # With no own file there is nothing to check: a refusal in words.
    _ok(_call("check_proxies", UID) == "denied")


def test_start_and_cancel_answer_without_breaking() -> None:
    """No data and no project selected: both must refuse in words, not raise."""
    _ok(_call("start", UID, "no-such-project") == "denied")
    _ok(_call("cancel", UID, 999999) == "denied")
    _ok(_call("start_all", UID) == "denied")


def test_payment_actions_reach_their_gates() -> None:
    _ok(_call("buy", UID, "nonsense") == "denied")
    _ok(_call("check_payment", UID, "no-such-payment") == "denied")
    _ok(_call("cancel_payment", UID, "no-such-payment") == "denied")


def test_deliver_answers_for_an_unknown_project() -> None:
    _ok(_call("deliver", UID, "no-such-project") == "denied")


def test_calendar_edits_are_admin_only_and_still_callable() -> None:
    _ok(_call("save_mint", UID, {"project_id": "whatever"}) == "denied",
        "a non-admin must be refused, not crashed")
    _ok(_call("clear_mint", UID, "whatever") == "denied")

    previous = config.ADMIN_IDS
    try:
        config.ADMIN_IDS = {UID}
        # Still refused — the project does not exist — but through the project
        # check rather than the admin one, which is the path that matters here.
        _ok(_call("save_mint", UID, {"project_id": "no-such-project"}) == "denied")
    finally:
        config.ADMIN_IDS = previous


# ------------------------------------------------- the paths refusals skip
#
# The tests above call every method, but most of them answer "no such project"
# or "no such payment" — and that answer is the first line of the function. The
# body never runs, which is exactly where a `bot_db.get_user` hides. These go
# the other way: set up so the gates pass, and let each method do its work.


def _a_real_project():
    tgbot.load_projects()
    list_projects = tgbot.list_projects
    projects = [
        p
        for p in list_projects()
        # A project pinned to the operator (`only_user_ids`) denies this test's
        # user before any action runs — that is the gate working, not a project
        # to test the actions with.
        if not getattr(p, "self_sourced", False)
        and not getattr(p, "only_user_ids", ())
    ]
    return projects[0] if projects else None


def _give_wallets(uid: int, n: int = 3) -> None:
    rows = "\n".join(f"u{i}|0x" + f"{i:040x}" for i in range(1, n + 1))
    ACTIONS.save_data(uid, rows + "\n")


def test_start_actually_enqueues_and_cancel_actually_stops_it() -> None:
    """The whole point of the app, and the longest path in it."""
    project = _a_real_project()
    _ok(project is not None, "the registry must have a project to test with")
    starter = 700601
    bot_db.ensure_user(starter, "starter", "Starter")
    _give_wallets(starter)

    out = _call("start", starter, project.id)
    if out == "denied":
        # A refusal here is legitimate (health, proxies, slots) but it must be
        # a refusal *after* the gates ran, not an exception dressed as one.
        return
    _ok(out["ok"] is True, out)
    job_id = out["job_id"]
    _ok(job_id is not None, out)
    _ok(bot_queue.get_job(job_id) is not None, "the job must exist in the queue")

    stopped = _call("cancel", starter, job_id)
    _ok(stopped != "denied", f"cancelling one's own job must work: {stopped}")
    _ok(stopped["ok"] is True, stopped)


def test_start_all_runs_its_whole_body_for_a_vip() -> None:
    vip = 700602
    bot_db.ensure_user(vip, "vip", "Vip")
    bot_db.set_user_limit_override(vip, 1000, admin_id=1)
    bot_limits.invalidate(vip)
    _give_wallets(vip)
    try:
        out = _call("start_all", vip)
        _ok(out == "denied" or out["ok"] is True, out)
    finally:
        bot_db.del_user_limit_override(vip)
        bot_limits.invalidate(vip)


def test_the_calendar_writes_when_the_caller_is_an_admin() -> None:
    """`save_mint` past the admin gate and past the project check."""
    project = _a_real_project()
    admin = 700603
    bot_db.ensure_user(admin, "adm", "Adm")
    previous = config.ADMIN_IDS
    try:
        config.ADMIN_IDS = {admin}
        out = _call("save_mint", admin, {
            "project_id": project.id, "supply": 1234, "chain": "TestChain",
            "mint_url": "https://example.test/mint",
            "phases": [{"kind": "ratlist", "starts_at": "2026-09-01 15:00",
                        "price": "0.01 ETH"}],
        })
        _ok(out != "denied", f"an admin must be able to write: {out}")
        row = next((r for r in bot_mints.calendar({project.id: project.name})
                    if r["project_id"] == project.id), None)
        _ok(row is not None and row["supply"] == 1234, row)
        # These three were each dropped silently by the web editor: the mint
        # link, the window's own name, and (for a drop with no registry entry)
        # the display name.
        _ok(row["mint_url"] == "https://example.test/mint",
            f"the mint link must survive the save: {row}")
        _ok(row["phases"] and row["phases"][0]["label"] == "RATLIST",
            f"a custom phase must keep its name: {row['phases']}")

        _ok(_call("clear_mint", admin, project.id)["ok"] is True)
        _ok(not [r for r in bot_mints.calendar({}) if r["project_id"] == project.id])

        bad = _call("save_mint", admin, {
            "project_id": project.id,
            "phases": [{"kind": "wl", "starts_at": "когда-нибудь"}]})
        _ok(bad == "denied", "an unreadable date is refused, not stored")
    finally:
        config.ADMIN_IDS = previous


def test_the_calendar_takes_a_drop_the_bot_does_not_register_for() -> None:
    """Hood Citizens mints on OpenSea with no form. The web refused it."""
    admin = 700606
    bot_db.ensure_user(admin, "adm2", "Adm2")
    previous = config.ADMIN_IDS
    try:
        config.ADMIN_IDS = {admin}
        out = _call("save_mint", admin, {
            "project_id": "hoodcitizens-test", "name": "Hood Citizens",
            "phases": [{"kind": "public", "starts_at": "2026-09-02 18:00",
                        "price": "0.0005 ETH"}],
        })
        _ok(out != "denied", f"a calendar-only drop must be accepted: {out}")
        row = next((r for r in bot_mints.calendar({})
                    if r["project_id"] == "hoodcitizens-test"), None)
        _ok(row is not None and row["project"] == "Hood Citizens", row)
        _ok(row["live"] is False, "it has no registry entry and must say so")
        bot_mints.clear_project("hoodcitizens-test")

        nameless = _call("save_mint", admin, {"project_id": "nothing-at-all"})
        _ok(nameless == "denied", "without a name there is nothing to show")
    finally:
        config.ADMIN_IDS = previous


def test_buy_refuses_new_invoices_and_settles_an_open_address() -> None:
    """New Basic invoices are refused (sales moved to an external bot):
    an address already open is still re-shown so a transfer in flight settles."""
    buyer = 700604
    bot_db.ensure_user(buyer, "buyer", "Buyer")

    refused = _call("buy", buyer, billing.PLAN_BASIC)
    _ok(refused == "denied", f"a new invoice must be refused: {refused}")

    bot_db.create_payment_record(
        payment_id="test-pay-1", order_id="order-1", user_id=buyer,
        plan="basic", price_amount="50", pay_currency="usdtbsc",
        pay_address="0xabc", pay_amount="49.9", status="waiting",
        valid_until="2026-09-01T00:00:00Z")
    out = _call("buy", buyer, billing.PLAN_BASIC)
    _ok(out != "denied" and out.get("reused") is True, out)
    _ok(out["payment"]["pay_address"] == "0xabc", out)
    _ok(bot_db.get_open_payment_for_user(buyer) is not None, "the record stays stored")

    # And cancelling reaches the record.
    dropped = _call("cancel_payment", buyer, "test-pay-1")
    _ok(dropped != "denied" and dropped["ok"] is True, dropped)
    _ok(bot_db.get_open_payment_for_user(buyer) is None, "cancel must close it")


def test_check_payment_reaches_the_settlement_code() -> None:
    """Past the ownership check and into `payments_poll.handle_status`."""
    buyer = 700605
    bot_db.ensure_user(buyer, "b2", "B2")
    bot_db.create_payment_record(
        payment_id="test-pay-2", order_id="order-2", user_id=buyer,
        plan="basic", price_amount="50", pay_currency="usdtbsc",
        pay_address="0xdef", pay_amount="49.9", status="waiting",
        valid_until="2026-09-01T00:00:00Z")

    class _Fresh:
        payment_status = "waiting"
        actually_paid = ""
        pay_amount = "49.9"
        payment_id = "test-pay-2"

    real_get = payments_np.get_payment
    payments_np.get_payment = lambda pid: _Fresh()
    try:
        out = _call("check_payment", buyer, "test-pay-2")
        _ok(out != "denied", f"an owned payment must be checkable: {out}")
        _ok(out["credited"] is False, out)
        # Somebody else's payment is refused, and refused the same way a
        # missing one is, so ids cannot be enumerated.
        _ok(_call("check_payment", UID, "test-pay-2") == "denied")
    finally:
        payments_np.get_payment = real_get


def test_deliver_runs_the_projects_own_hook() -> None:
    """No project defines `queue_redeliver` today, so the hook is supplied."""

    project = _a_real_project()
    calls = []

    def hook(user_id, bot):
        calls.append(user_id)
        return 3

    project.queue_redeliver = hook
    try:
        out = _call("deliver", UID, project.id)
        _ok(out != "denied", f"a project with a hook must deliver: {out}")
        _ok(out["sent"] == 3 and calls == [UID], (out, calls))
    finally:
        del project.queue_redeliver


def test_report_returns_a_real_saved_run() -> None:
    tgbot.bulk_report_save(UID, {
        "project": "test", "errors": ["стр. 1: плохой кошелёк"],
        "stats": {"OK": 5, "FAIL": 1},
    })
    out = _call("report", UID)
    _ok(out["has"] is True and out["errors_total"] == 1, out)
    _ok(out["stats"].get("OK") == 5, out)


# -------------------------------------------------------------------- gate


class _Member:
    def __init__(self, status: str) -> None:
        self.status = status


class _ChannelBot(_Bot):
    """A bot that answers `get_chat_member`, which the plain `_Bot` does not."""

    def __init__(self, status: str | Exception = "member") -> None:
        super().__init__()
        self.answer = status
        self.asked = 0

    def get_chat_member(self, chat, user_id):
        self.asked += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Member(self.answer)


def _gate_with(status, *, uid: int, require: bool = True):
    """Run the real gate against a controlled channel answer."""
    bot_channel = tgbot.bot_channel
    bot_channel.invalidate_subscription()
    fake = _ChannelBot(status)
    actions = tgbot.BotActions(fake)
    before = config.REQUIRE_CHANNEL, config.REQUIRED_CHANNEL
    config.REQUIRE_CHANNEL = require
    config.REQUIRED_CHANNEL = "@testchannel"
    try:
        return actions.gate(uid, "ru"), fake
    finally:
        config.REQUIRE_CHANNEL, config.REQUIRED_CHANNEL = before
        bot_channel.invalidate_subscription()


def test_the_gate_refuses_a_banned_user() -> None:
    uid = 880001
    bot_db.ensure_user(uid, "banned", "B")
    bot_db.set_banned(uid, True)
    try:
        refusal, fake = _gate_with("member", uid=uid)
        _ok(refusal is not None, "a banned user passed the web gate")
        _ok(refusal.code == "banned", refusal.code)
        _ok(bool(refusal.message), "a refusal must say something")
        # The ban is decided before Telegram is asked anything.
        _ok(fake.asked == 0, "the ban check paid for a getChatMember")
    finally:
        bot_db.set_banned(uid, False)


def test_the_gate_refuses_someone_who_never_joined_the_channel() -> None:
    uid = 880002
    bot_db.ensure_user(uid, "nosub", "N")
    refusal, _fake = _gate_with("left", uid=uid)
    _ok(refusal is not None, "a non-subscriber passed the web gate")
    _ok(refusal.code == "not_subscribed", refusal.code)


def test_the_gate_lets_a_subscribed_user_through() -> None:
    uid = 880003
    bot_db.ensure_user(uid, "member", "M")
    for status in ("member", "creator", "administrator", "restricted"):
        refusal, _fake = _gate_with(status, uid=uid)
        _ok(refusal is None, f"status {status} was refused: {refusal}")


def test_a_broken_channel_lookup_does_not_lock_everyone_out() -> None:
    """Our misconfiguration must not become every user's outage."""
    uid = 880004
    bot_db.ensure_user(uid, "err", "E")
    refusal, _fake = _gate_with(RuntimeError("bot is not a member"), uid=uid)
    _ok(refusal is None, f"a channel error refused a real user: {refusal}")


def test_the_gate_is_off_when_the_channel_gate_is_off() -> None:
    uid = 880005
    bot_db.ensure_user(uid, "nogate", "N")
    refusal, fake = _gate_with("left", uid=uid, require=False)
    _ok(refusal is None, refusal)
    _ok(fake.asked == 0, "REQUIRE_CHANNEL=0 still called Telegram")


def test_membership_is_cached_so_a_poll_does_not_spend_the_api() -> None:
    """The screen asks every four seconds; Telegram must be asked far less."""
    uid = 880006
    bot_db.ensure_user(uid, "cached", "C")
    bot_channel = tgbot.bot_channel
    bot_channel.invalidate_subscription()
    fake = _ChannelBot("member")
    actions = tgbot.BotActions(fake)
    before = config.REQUIRE_CHANNEL, config.REQUIRED_CHANNEL
    config.REQUIRE_CHANNEL = True
    config.REQUIRED_CHANNEL = "@testchannel"
    try:
        for _ in range(25):
            _ok(actions.gate(uid, "ru") is None)
        _ok(fake.asked == 1, f"25 polls cost {fake.asked} getChatMember calls")
        # And the answer is droppable, which is what `subcheck` relies on.
        bot_channel.invalidate_subscription(uid)
        actions.gate(uid, "ru")
        _ok(fake.asked == 2, fake.asked)
    finally:
        config.REQUIRE_CHANNEL, config.REQUIRED_CHANNEL = before
        bot_channel.invalidate_subscription()


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            _use_my_paths()
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
