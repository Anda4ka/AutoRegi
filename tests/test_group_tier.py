"""Group membership as a tier source (bot/group_tier.py + the limits ladder).

Nothing here talks to Telegram: `get_chat_member` is a stub whose call count is
part of the assertions, because the whole point of the cache is that a render
does not ask Telegram again.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="group_tier_test_"))
os.environ["BOT_DB"] = str(_TMP / "test.db")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["SUBSCRIBERS_FILE"] = str(_TMP / "active_subscribers.txt")
os.environ["FREE_OK_LIMIT"] = "10"
os.environ["SUBSCRIBER_OK_LIMIT"] = "50"
os.environ["SUBSCRIBER_GROUP_OK_LIMIT"] = "50"
os.environ["SUBSCRIBER_GROUP"] = "-1001111111111"
os.environ["LIMITS_CACHE_TTL"] = "0"
os.environ["PAYMENTS_ENABLED"] = "0"
os.environ["BOT_TOKEN"] = "test:token"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import group_tier  # noqa: E402
from bot import limits as bot_limits  # noqa: E402

bot_db.init_db()

GROUP = "-1001111111111"


class FakeMember:
    def __init__(self, status: str, is_member: bool = True) -> None:
        self.status = status
        self.is_member = is_member


class FakeBot:
    """Counts calls and answers per user id; may raise a Telegram-ish error."""

    def __init__(self, answers: dict[int, object]) -> None:
        self.answers = answers
        self.calls = 0

    def get_chat_member(self, chat_id, user_id):
        self.calls += 1
        assert str(chat_id) == GROUP, f"asked the wrong chat: {chat_id}"
        answer = self.answers[int(user_id)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class ApiError(Exception):
    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


def _bind(bot: FakeBot) -> None:
    group_tier.invalidate()
    bot_limits.invalidate()
    group_tier.bind(bot)


def test_member_gets_subscriber_limit() -> None:
    uid = 900001
    bot = FakeBot({uid: FakeMember("member")})
    _bind(bot)
    assert group_tier.is_member(uid) is True
    caps = bot_limits.tier_caps(uid, fresh=True)
    assert caps.ok_limit == 50, caps
    assert caps.mode == "subscriber", caps


def test_non_member_stays_free() -> None:
    uid = 900002
    bot = FakeBot({uid: FakeMember("left")})
    _bind(bot)
    assert group_tier.is_member(uid) is False
    caps = bot_limits.tier_caps(uid, fresh=True)
    assert caps.ok_limit == int(bot_db.get_free_limit()) == 10, caps
    assert caps.mode == "free", caps


def test_restricted_that_left_is_not_a_member() -> None:
    uid = 900003
    bot = FakeBot({uid: FakeMember("restricted", is_member=False)})
    _bind(bot)
    assert group_tier.is_member(uid) is False


def test_answer_is_cached_and_invalidated() -> None:
    uid = 900004
    bot = FakeBot({uid: FakeMember("administrator")})
    _bind(bot)
    for _ in range(5):
        assert group_tier.is_member(uid) is True
    assert bot.calls == 1, f"cache did not hold: {bot.calls} calls"
    group_tier.invalidate(uid)
    assert group_tier.is_member(uid) is True
    assert bot.calls == 2


def test_user_not_found_is_a_real_no() -> None:
    uid = 900005
    bot = FakeBot({uid: ApiError("Bad Request: user not found")})
    _bind(bot)
    assert group_tier.is_member(uid) is False
    # A real answer is cached like any other.
    assert group_tier.is_member(uid) is False
    assert bot.calls == 1


def test_lost_rights_keeps_the_previous_answer() -> None:
    """Our failure must not demote a member to free while it is being fixed."""
    uid = 900006
    member = FakeMember("member")
    bot = FakeBot({uid: member})
    _bind(bot)
    assert group_tier.is_member(uid) is True
    bot.answers[uid] = ApiError("Forbidden: bot is not a member of the supergroup chat")
    # Expire the entry the way time would: the value stays, its deadline does not.
    with group_tier._lock:
        group_tier._cache[uid] = (time.monotonic() - 1.0, True)
    assert group_tier.is_member(uid) is True, "stale yes must survive an API failure"
    # And with nothing remembered at all the answer is a plain no, never a crash.
    group_tier.invalidate()
    assert group_tier.is_member(uid) is False


def test_join_and_leave_events_apply_immediately() -> None:
    uid = 900007
    bot = FakeBot({uid: FakeMember("left")})
    _bind(bot)
    assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 10
    group_tier.note_membership(uid, True)
    assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 50
    assert bot.calls == 1, "an event answers the question without an API call"
    group_tier.note_membership(uid, False)
    assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 10


class FakeChat:
    def __init__(self, cid, ctype="supergroup", username=None):
        self.id = cid
        self.type = ctype
        self.username = username


class FakeUser:
    def __init__(self, uid, is_bot=False):
        self.id = uid
        self.is_bot = is_bot


class FakeMessage:
    def __init__(self, chat, new_chat_members=None, left_chat_member=None):
        self.chat = chat
        self.new_chat_members = new_chat_members
        self.left_chat_member = left_chat_member


def test_service_message_grants_and_revokes() -> None:
    uid = 900010
    bot = FakeBot({uid: FakeMember("left")})
    _bind(bot)
    group = FakeChat(int(GROUP))
    assert group_tier.note_service_message(
        FakeMessage(group, new_chat_members=[FakeUser(uid)])
    ) is True
    assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 50
    assert bot.calls == 0, "a service message must not cost an API call"
    assert group_tier.note_service_message(
        FakeMessage(group, left_chat_member=FakeUser(uid))
    ) is True
    assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 10


def test_service_message_from_another_chat_is_ignored() -> None:
    uid = 900011
    bot = FakeBot({uid: FakeMember("left")})
    _bind(bot)
    other = FakeChat(-1009999999999)
    assert group_tier.note_service_message(
        FakeMessage(other, new_chat_members=[FakeUser(uid)])
    ) is False
    assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 10


def test_a_bot_joining_grants_nothing() -> None:
    uid = 900012
    bot = FakeBot({uid: FakeMember("left")})
    _bind(bot)
    assert group_tier.note_service_message(
        FakeMessage(FakeChat(int(GROUP)), new_chat_members=[FakeUser(uid, is_bot=True)])
    ) is False


def test_file_and_group_add_up() -> None:
    """Both sources at once is 50 + 50, not 50 — the operator asked for it."""
    from unittest.mock import patch

    uid = 900013
    bot_limits.invalidate(uid)
    with patch.object(bot_limits.sub, "is_subscriber", return_value=True), patch.object(
        bot_limits.group_tier, "enabled", return_value=True
    ), patch.object(bot_limits.group_tier, "is_member", return_value=True), patch.object(
        bot_limits.group_tier, "member_ok_limit", return_value=50
    ):
        caps = bot_limits.tier_caps(uid, fresh=True)
        assert caps.ok_limit == 100, caps
        assert caps.mode == "subscriber", caps
    with patch.object(bot_limits.sub, "is_subscriber", return_value=True), patch.object(
        bot_limits.group_tier, "enabled", return_value=True
    ), patch.object(bot_limits.group_tier, "is_member", return_value=False), patch.object(
        bot_limits.group_tier, "member_ok_limit", return_value=50
    ):
        bot_limits.invalidate(uid)
        assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 50


def test_group_alone_is_one_tier_not_two() -> None:
    uid = 900014
    bot = FakeBot({uid: FakeMember("member")})
    _bind(bot)
    assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 50


def test_group_never_lowers_a_higher_tier() -> None:
    uid = 900008
    bot = FakeBot({uid: FakeMember("left")})
    _bind(bot)
    bot_db.set_user_limit_override(uid, 1000)
    try:
        caps = bot_limits.tier_caps(uid, fresh=True)
        assert caps.ok_limit == 1000 and caps.mode == "vip", caps
    finally:
        bot_db.del_user_limit_override(uid)
        bot_limits.invalidate(uid)


def test_matches_chat_accepts_id_and_username() -> None:
    assert group_tier.matches_chat(-1001111111111) is True
    assert group_tier.matches_chat(-1000000000001) is False
    assert group_tier.matches_chat(None, "somegroup") is False


def test_disabled_group_asks_nobody() -> None:
    uid = 900009
    bot = FakeBot({uid: FakeMember("member")})
    _bind(bot)
    saved = config.SUBSCRIBER_GROUP
    config.SUBSCRIBER_GROUP = ""
    try:
        assert group_tier.enabled() is False
        assert group_tier.is_member(uid) is False
        assert bot.calls == 0
        assert bot_limits.tier_caps(uid, fresh=True).ok_limit == 10
    finally:
        config.SUBSCRIBER_GROUP = saved
        group_tier.invalidate()
        bot_limits.invalidate()


def main() -> None:
    tests = [
        test_member_gets_subscriber_limit,
        test_non_member_stays_free,
        test_restricted_that_left_is_not_a_member,
        test_answer_is_cached_and_invalidated,
        test_user_not_found_is_a_real_no,
        test_lost_rights_keeps_the_previous_answer,
        test_join_and_leave_events_apply_immediately,
        test_service_message_grants_and_revokes,
        test_service_message_from_another_chat_is_ignored,
        test_a_bot_joining_grants_nothing,
        test_file_and_group_add_up,
        test_group_alone_is_one_tier_not_two,
        test_group_never_lowers_a_higher_tier,
        test_matches_chat_accepts_id_and_username,
        test_disabled_group_asks_nobody,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
