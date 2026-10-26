"""Own /proxy dead-exit alert: 20 consecutive proxy FAILs → one DM per job."""

from __future__ import annotations

from bot.http_client import is_proxy_fail_detail
from bot import submit_queue as sq


def test_is_proxy_fail_detail_matches_format_request_error() -> None:
    assert is_proxy_fail_detail(
        "proxy error via 203.0.113.7:6675: ProxyError"
    )
    assert is_proxy_fail_detail("proxy refused connection 1.2.3.4:8080")
    assert is_proxy_fail_detail("proxy connect timeout 1.2.3.4:8080")
    assert not is_proxy_fail_detail("connect timeout via 1.2.3.4:8080")
    assert not is_proxy_fail_detail("no_proxies: empty proxy pool")
    assert not is_proxy_fail_detail("HTTP 403: Access denied")
    assert not is_proxy_fail_detail("")


def test_streak_fires_once_at_threshold_and_only_for_own() -> None:
    sent: list[tuple] = []

    class Bot:
        def send_message(self, chat_id, text, **_kwargs):
            sent.append((chat_id, text))

            class M:
                message_id = 1

            return M()

    job = sq.QueueJob(
        id=1,
        user_id=42,
        project_id="example",
        chat_id=42,
        message_id=None,
        status="running",
    )
    job.uses_own_proxies = True
    prev_bot = sq._bot
    sq._bot = Bot()
    try:
        proxy_fail = {
            "status": "FAIL",
            "detail": "proxy error via 1.2.3.4:8080: ProxyError",
        }
        for _ in range(sq.OWN_PROXY_FAIL_ALERT_STREAK - 1):
            sq._note_own_proxy_fail(job, proxy_fail, lang="ru")
        assert not job.own_proxy_alert_sent
        assert not sent
        assert job.own_proxy_fail_streak == sq.OWN_PROXY_FAIL_ALERT_STREAK - 1

        sq._note_own_proxy_fail(job, proxy_fail, lang="ru")
        assert job.own_proxy_alert_sent
        assert len(sent) == 1
        assert sent[0][0] == 42
        assert "прокси" in sent[0][1].lower()
        assert "/proxy" in sent[0][1]

        # Once per job — further proxy FAILs stay silent.
        sq._note_own_proxy_fail(job, proxy_fail, lang="ru")
        assert len(sent) == 1
    finally:
        sq._bot = prev_bot


def test_shared_pool_and_non_proxy_fail_do_not_alert() -> None:
    sent: list = []

    class Bot:
        def send_message(self, *_a, **_k):
            sent.append(1)

            class M:
                message_id = 1

            return M()

    job = sq.QueueJob(1, 7, "example", 7, None, "running")
    job.uses_own_proxies = False
    prev_bot = sq._bot
    sq._bot = Bot()
    try:
        fail = {"status": "FAIL", "detail": "proxy error via 1.2.3.4:1: ProxyError"}
        for _ in range(sq.OWN_PROXY_FAIL_ALERT_STREAK + 5):
            sq._note_own_proxy_fail(job, fail, lang="en")
        assert not sent
        assert not job.own_proxy_alert_sent

        job.uses_own_proxies = True
        for _ in range(10):
            sq._note_own_proxy_fail(job, fail, lang="en")
        # Non-proxy FAIL resets the streak.
        sq._note_own_proxy_fail(
            job, {"status": "FAIL", "detail": "HTTP 500: boom"}, lang="en"
        )
        assert job.own_proxy_fail_streak == 0
        for _ in range(10):
            sq._note_own_proxy_fail(job, fail, lang="en")
        assert not sent
        assert job.own_proxy_fail_streak == 10

        # OK also resets.
        sq._note_own_proxy_fail(job, {"status": "OK", "detail": "ok"}, lang="en")
        assert job.own_proxy_fail_streak == 0
    finally:
        sq._bot = prev_bot
