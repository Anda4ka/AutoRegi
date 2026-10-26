"""Hard test-suite guard: never let imports resolve to production state."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path


_TEST_ROOT = Path(tempfile.mkdtemp(prefix="autoreg_pytest_"))
_STATE = _TEST_ROOT / "state"

os.environ.update(
    {
        "AUTOREG_STATE_DIR": str(_STATE),
        "AUTOREG_MIGRATE_LEGACY": "0",
        "BOT_DB": str(_STATE / "tgbot.db"),
        "BOT_RESULTS": str(_STATE / "bot_results.jsonl"),
        "PROXY_FILE": str(_STATE / "proxy.txt"),
        "SUBSCRIBERS_FILE": str(_STATE / "active_subscribers.txt"),
        "USER_PROXY_DIR": str(_STATE / "user_proxies"),
        "USER_DATA_DIR": str(_STATE / "user_data"),
        "JOB_PAYLOAD_DIR": str(_STATE / "job_payloads"),
        "BOT_TOKEN": "test:token",
        # No test may reach a payment API or see a cached tier.
        "PAYMENTS_ENABLED": "0",
        # No test may reach the captcha solver either: the key in .env is real
        # and every solve costs money. A test that needs a token stubs
        # `captcha.solve_turnstile`; everything else must see "not configured".
        "CAPSOLVER_API_KEY": "",
        "TWOCAPTCHA_API_KEY": "",
        # Same rule for X, and it was not hypothetical: a test once resolved
        # the real client through the already-imported package attribute and
        # read two live timelines — 40 posts at $0.005 each, billed to the
        # operator. Without a bearer nothing can be read.
        "X_BEARER_TOKEN": "",
        "NOWPAYMENTS_API_KEY": "",
        "NOWPAYMENTS_SANDBOX": "1",
        "LIMITS_CACHE_TTL": "0",
        "PROJECTS_PER_PAGE": "8",
    }
)


@atexit.register
def _remove_test_state() -> None:
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
