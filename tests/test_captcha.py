"""Turnstile solver client (`bot/captcha.py`).

These used to live beside the submit path of the project that forced this
module into existence. That project was removed in the 2026-08-15 audit; the
solver is shared infrastructure and its edges still have to hold:

  * the proxyless task type is the default, and a proxy switches to the paid one;
  * «no balance» / «bad key» are named in the detail so a run can be diagnosed;
  * the API key never leaks into a detail string;
  * a provider out of money is stepped over, and its «no balance» never
    becomes the answer while another account is still answering — that
    would stop a row the second account could have filed.

The single-use-token and 403-retry rules that the rest of that file pinned were
specific to that project's route and went with it. They are the expensive half of
the lesson — a token is single-use, and a site's 403 is a fresh-solve retry
rather than a verdict — so the next adapter that solves a captcha owes them
coverage of its own.

No network: `requests` is stubbed inside the module.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="captcha_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "t:t"
os.environ["CAPSOLVER_API_KEY"] = "CAP-TEST-KEY"
os.environ["TWOCAPTCHA_API_KEY"] = "TWO-TEST-KEY"
# Pinned so these assertions describe the code and not whatever order the
# operator has in `.env` today.
os.environ["CAPTCHA_PROVIDER_ORDER"] = "capsolver,2captcha"
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")

# `tgbot` goes too: cached, it would stay bound to the bot modules dropped here
# and hand the next module a bot wired to this test's database.
for _mod in list(sys.modules):
    if (
        _mod == "bot"
        or _mod.startswith("bot.")
        or _mod.startswith("projects.")
        or _mod == "tgbot"
    ):
        del sys.modules[_mod]

from bot import captcha  # noqa: E402
from bot.http_client import is_captcha_solver_permanent_error  # noqa: E402


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or ""
        self.headers = {}
        self.reason = "test"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeCapsolver:
    """Scripted solver API: createTask → getTaskResult, for either provider.

    `create_error` may be keyed by provider name to script one account failing
    while the other answers.
    """

    def __init__(self, *, tokens=None, create_error=None, result_error=None, never_ready=False):
        self.tokens = list(tokens or ["token-1", "token-2", "token-3"])
        self.create_error = create_error
        self.result_error = result_error
        self.never_ready = never_ready
        self.created = 0
        self.bodies: list[dict] = []
        self.hosts: list[str] = []

    @staticmethod
    def _provider(url: str) -> str:
        return "2captcha" if "2captcha" in url else "capsolver"

    def _error_for(self, url):
        err = self.create_error
        if isinstance(err, dict) and set(err) <= {"capsolver", "2captcha"}:
            return err.get(self._provider(url))
        return err

    def post(self, url, **kw):
        body = kw.get("json") or {}
        self.bodies.append(body)
        self.hosts.append(self._provider(url))
        if url.endswith("/createTask"):
            err = self._error_for(url)
            if err:
                return FakeResponse(json_data={"errorId": 1, **err})
            self.created += 1
            return FakeResponse(json_data={"errorId": 0, "taskId": f"task-{self.created}"})
        if url.endswith("/getTaskResult"):
            if self.result_error:
                return FakeResponse(json_data={"errorId": 1, **self.result_error})
            if self.never_ready:
                return FakeResponse(json_data={"errorId": 0, "status": "processing"})
            token = self.tokens.pop(0) if self.tokens else "token-x"
            return FakeResponse(
                json_data={"errorId": 0, "status": "ready", "solution": {"token": token}}
            )
        if url.endswith("/getBalance"):
            return FakeResponse(json_data={"errorId": 0, "balance": 9.5})
        return FakeResponse(status_code=404, json_data={"errorId": 1})


def test_solver_returns_token() -> None:
    solver = FakeCapsolver(tokens=["abc123"])
    real, captcha.requests = captcha.requests, solver
    real_sleep, captcha.time.sleep = captcha.time.sleep, lambda *_a, **_k: None
    try:
        token, detail = captcha.solve_turnstile("https://example.test/", "0xKEY")
        _ok(token == "abc123", token)
        _ok(detail == "", detail)
        task = solver.bodies[0]["task"]
        _ok(task["type"] == "AntiTurnstileTaskProxyLess", str(task))
        _ok(task["websiteKey"] == "0xKEY", str(task))
        # With a proxy the task type switches to the paid variant.
        captcha.solve_turnstile("https://x/", "0xKEY", proxy_url="http://u:p@h:1")
        _ok(solver.bodies[-2]["task"]["type"] == "AntiTurnstileTask", str(solver.bodies[-2]))
    finally:
        captcha.requests, captcha.time.sleep = real, real_sleep


def test_solver_errors_never_leak_the_key() -> None:
    solver = FakeCapsolver(
        create_error={"errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "no funds"}
    )
    real, captcha.requests = captcha.requests, solver
    try:
        token, detail = captcha.solve_turnstile("https://example.test/", "0xKEY")
        _ok(token == "", token)
        _ok("ERROR_ZERO_BALANCE" in detail and "no funds" in detail, detail)
        _ok("CAP-TEST-KEY" not in detail, "api key must never appear in a detail")
    finally:
        captcha.requests = real


def test_second_provider_takes_over_when_the_first_is_out_of_money() -> None:
    """The whole point of a second account: the row still gets its token."""
    captcha.reset_dead_providers()
    solver = FakeCapsolver(
        tokens=["from-2captcha"],
        create_error={
            "capsolver": {"errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "no funds"},
        },
    )
    real, captcha.requests = captcha.requests, solver
    real_sleep, captcha.time.sleep = captcha.time.sleep, lambda *_a, **_k: None
    try:
        token, detail = captcha.solve_turnstile("https://example.test/", "0xKEY")
        _ok(token == "from-2captcha", token)
        _ok(detail == "", detail)
        _ok(solver.hosts[0] == "capsolver", str(solver.hosts))
        _ok("2captcha" in solver.hosts, str(solver.hosts))
        # 2Captcha's own task name, not Capsolver's.
        two = [b for b in solver.bodies if b.get("task", {}).get("type", "").startswith("Turnstile")]
        _ok(two and two[0]["task"]["type"] == "TurnstileTaskProxyless", str(solver.bodies))

        # The dead account is remembered: the next row does not pay to ask again.
        before = len(solver.hosts)
        captcha.solve_turnstile("https://example.test/", "0xKEY")
        _ok("capsolver" not in solver.hosts[before:], str(solver.hosts[before:]))
    finally:
        captcha.requests, captcha.time.sleep = real, real_sleep
        captcha.reset_dead_providers()


def test_dead_account_never_masks_a_live_one_s_transient_failure() -> None:
    """A timeout must stay retryable even next to somebody else's zero balance."""
    captcha.reset_dead_providers()
    solver = FakeCapsolver(
        create_error={
            "capsolver": {"errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "no funds"},
            "2captcha": {"errorCode": "ERROR_NO_SLOT_AVAILABLE", "errorDescription": "busy"},
        }
    )
    real, captcha.requests = captcha.requests, solver
    real_sleep, captcha.time.sleep = captcha.time.sleep, lambda *_a, **_k: None
    try:
        token, detail = captcha.solve_turnstile("https://example.test/", "0xKEY")
        _ok(token == "", token)
        _ok("ERROR_NO_SLOT_AVAILABLE" in detail, detail)
        # The permanent marker must not ride along: the caller reads the detail
        # to decide whether to stop the row for good.
        _ok("ZERO_BALANCE" not in detail, detail)
        _ok(not is_captcha_solver_permanent_error(detail), detail)
        _ok("TWO-TEST-KEY" not in detail and "CAP-TEST-KEY" not in detail, detail)
    finally:
        captcha.requests, captcha.time.sleep = real, real_sleep
        captcha.reset_dead_providers()


def test_every_account_dry_is_a_permanent_stop() -> None:
    captcha.reset_dead_providers()
    solver = FakeCapsolver(
        create_error={"errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "no funds"}
    )
    real, captcha.requests = captcha.requests, solver
    real_sleep, captcha.time.sleep = captcha.time.sleep, lambda *_a, **_k: None
    try:
        token, detail = captcha.solve_turnstile("https://example.test/", "0xKEY")
        _ok(token == "", token)
        _ok(is_captcha_solver_permanent_error(detail), detail)
        _ok(detail.count("ZERO_BALANCE") == 2, detail)
    finally:
        captcha.requests, captcha.time.sleep = real, real_sleep
        captcha.reset_dead_providers()


def test_proxy_is_split_into_the_fields_2captcha_wants() -> None:
    captcha.reset_dead_providers()
    solver = FakeCapsolver(
        tokens=["t"],
        create_error={
            "capsolver": {"errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "no funds"},
        },
    )
    real, captcha.requests = captcha.requests, solver
    real_sleep, captcha.time.sleep = captcha.time.sleep, lambda *_a, **_k: None
    try:
        captcha.solve_turnstile(
            "https://x/", "0xKEY", proxy_url="http://user:pw@1.2.3.4:8080"
        )
        cap = [b for b in solver.bodies if b.get("task", {}).get("type", "").startswith("Anti")]
        two = [b for b in solver.bodies if b.get("task", {}).get("type", "").startswith("Turnstile")]
        # Capsolver takes the URL whole; 2Captcha takes it in five pieces.
        _ok(cap[0]["task"]["proxy"] == "http://user:pw@1.2.3.4:8080", str(cap[0]))
        task = two[0]["task"]
        _ok(task["type"] == "TurnstileTask", str(task))
        _ok(task["proxyAddress"] == "1.2.3.4" and task["proxyPort"] == 8080, str(task))
        _ok(task["proxyLogin"] == "user" and task["proxyPassword"] == "pw", str(task))
    finally:
        captcha.requests, captcha.time.sleep = real, real_sleep
        captcha.reset_dead_providers()


def test_recaptcha_v2_task_and_token_shape() -> None:
    """reCAPTCHA v2 (Netlify Forms class): right task type, token read from
    solution.gRecaptchaResponse, and a fresh solve per attempt."""

    class FakeRecaptcha(FakeCapsolver):
        def post(self, url, **kw):
            resp = super().post(url, **kw)
            if url.endswith("/getTaskResult"):
                sol = getattr(resp, "_json", None)
                if isinstance(sol, dict):
                    inner = sol.get("solution")
                    if isinstance(inner, dict) and "token" in inner:
                        inner["gRecaptchaResponse"] = inner.pop("token")
            return resp

    solver = FakeRecaptcha(tokens=["rec-token-1"])
    real, captcha.requests = captcha.requests, solver
    real_sleep, captcha.time.sleep = captcha.time.sleep, lambda *_a, **_k: None
    try:
        token, detail = captcha.solve_recaptcha_v2(
            "https://demoi.test/apply", "6LdnkKEY"
        )
        _ok(token == "rec-token-1", token)
        _ok(detail == "", detail)
        task = solver.bodies[0]["task"]
        _ok(task["type"] == "ReCaptchaV2TaskProxyLess", str(task))
        _ok(task["websiteKey"] == "6LdnkKEY", str(task))
        captcha.solve_recaptcha_v2(
            "https://demoi.test/apply", "6LdnkKEY", proxy_url="http://u:p@h:1"
        )
        _ok(solver.bodies[-2]["task"]["type"] == "ReCaptchaV2Task", str(solver.bodies[-2]))
    finally:
        captcha.requests, captcha.time.sleep = real, real_sleep


def main() -> None:
    tests = [
        test_solver_returns_token,
        test_solver_errors_never_leak_the_key,
        test_second_provider_takes_over_when_the_first_is_out_of_money,
        test_dead_account_never_masks_a_live_one_s_transient_failure,
        test_every_account_dry_is_a_permanent_stop,
        test_proxy_is_split_into_the_fields_2captcha_wants,
        test_recaptcha_v2_task_and_token_shape,
    ]
    failed = 0
    for tfn in tests:
        try:
            tfn()
            print(f"PASS {tfn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {tfn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
