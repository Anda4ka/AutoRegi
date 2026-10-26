"""Per-user cooldown on incoming messages (config.MESSAGE_COOLDOWN_SEC)."""

from __future__ import annotations

from bot import rate_limit


def _uid(n: int) -> int:
    return 900_000_000 + n


def test_first_message_passes_then_window_closes() -> None:
    uid = _uid(1)
    rate_limit.reset_user(uid)
    assert rate_limit.message_gate(uid, 60.0)[0] is True
    ok, left, _warn = rate_limit.message_gate(uid, 60.0)
    assert ok is False
    assert 0 < left <= 60.0


def test_only_the_first_blocked_message_is_answered() -> None:
    """A flood must cost one reply per window, not one reply per message."""
    uid = _uid(2)
    rate_limit.reset_user(uid)
    rate_limit.message_gate(uid, 60.0)
    assert rate_limit.message_gate(uid, 60.0)[2] is True
    for _ in range(20):
        assert rate_limit.message_gate(uid, 60.0)[2] is False


def test_expired_window_lets_the_next_message_through() -> None:
    uid = _uid(3)
    rate_limit.reset_user(uid)
    assert rate_limit.message_gate(uid, 0.05)[0] is True
    assert rate_limit.message_gate(uid, 0.05)[0] is False
    import time

    time.sleep(0.06)
    assert rate_limit.message_gate(uid, 0.05)[0] is True


def test_zero_cooldown_disables_the_gate() -> None:
    uid = _uid(4)
    rate_limit.reset_user(uid)
    for _ in range(5):
        assert rate_limit.message_gate(uid, 0.0) == (True, 0.0, False)


def test_reset_user_clears_the_gate() -> None:
    uid = _uid(5)
    rate_limit.reset_user(uid)
    rate_limit.message_gate(uid, 60.0)
    assert rate_limit.message_gate(uid, 60.0)[0] is False
    rate_limit.reset_user(uid)
    assert rate_limit.message_gate(uid, 60.0)[0] is True


def test_gate_prunes_stale_users() -> None:
    """The dict must not grow without bound on a long-running poller."""
    rate_limit._msg_gate.clear()
    for n in range(rate_limit._MSG_GATE_MAX + 50):
        rate_limit._msg_gate[n] = (0.0, 0.0)  # last seen at epoch = stale
    rate_limit.message_gate(_uid(6), 60.0)
    assert len(rate_limit._msg_gate) == 1


def test_allow_drops_expired_user_entries() -> None:
    """A user whose window is empty must not stay resident in `_hits`."""
    uid = _uid(7)
    rate_limit.reset_user(uid)
    assert rate_limit.allow(uid, "pay_check")[0] is True
    assert uid in rate_limit._hits["pay_check"]
    # Age the single entry past the window: the next allow() prunes it, and a
    # user who never returns leaves nothing behind once the sweep runs.
    rate_limit._hits["pay_check"][uid].popleft()
    rate_limit._hits["pay_check"][uid].append(rate_limit.time.time() - 9999)
    assert rate_limit.allow(uid, "pay_check")[0] is True
    assert len(rate_limit._hits["pay_check"][uid]) == 1


def test_hits_sweep_prunes_stale_users() -> None:
    """Above the user cap, stale entries are swept instead of growing forever."""
    rate_limit._hits.clear()
    rate_limit._hits_last_prune = 0.0
    rate_limit._HITS_USERS_MAX = 20
    stale = rate_limit.time.time() - 9999
    for i in range(25):
        rate_limit._hits["pay_check"][_uid(100 + i)].append(stale)
    assert sum(len(b) for b in rate_limit._hits.values()) == 25
    assert rate_limit.allow(_uid(200), "pay_check")[0] is True
    assert _uid(100) not in rate_limit._hits.get("pay_check", {}), "stale swept"
    rate_limit._HITS_USERS_MAX = 10_000
