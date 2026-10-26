"""NOWPayments client: base URL, parsing, retry policy. No network."""

from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="np_test_"))
_DB = _TMP / "t.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["NOWPAYMENTS_SANDBOX"] = "1"
os.environ["NOWPAYMENTS_API_KEY"] = "test-key"
os.environ.pop("NOWPAYMENTS_BASE_URL", None)

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import billing  # noqa: E402
from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import payments_np as np  # noqa: E402
import bot.db as dbmod  # noqa: E402
import requests  # noqa: E402

config.DB_PATH = _DB
dbmod.DB_PATH = _DB
bot_db.init_db()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (str(payload) if payload else "")
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeTransport:
    """Scripted requests.request replacement that records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        item = self.responses.pop(0) if self.responses else FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item


def _install(transport, monkey_sleep=True):
    requests.request = transport  # type: ignore[assignment]
    if monkey_sleep:
        np.time.sleep = lambda *_: None  # type: ignore[assignment]
    return transport


_REAL_REQUEST = requests.request


def _restore() -> None:
    requests.request = _REAL_REQUEST  # type: ignore[assignment]


PAYMENT_BODY = {
    "payment_id": "5745459419",
    "payment_status": "waiting",
    "pay_address": "0x" + "ab" * 20,
    "price_amount": 10,
    "price_currency": "usd",
    "pay_amount": "10.041234",
    "pay_currency": "usdtbsc",
    "order_id": "u1-pro-abc",
    "valid_until": "2026-08-13T10:00:00.000Z",
}


def test_api_base_sandbox_and_override() -> None:
    os.environ["NOWPAYMENTS_SANDBOX"] = "1"
    _assert(np.api_base() == np.SANDBOX_BASE, np.api_base())
    os.environ["NOWPAYMENTS_BASE_URL"] = "https://example.test/v1/"
    _assert(np.api_base() == "https://example.test/v1", np.api_base())
    os.environ.pop("NOWPAYMENTS_BASE_URL")
    _assert(np.api_base() == np.SANDBOX_BASE, "back to sandbox")


def test_missing_key_disables_the_feature() -> None:
    os.environ["NOWPAYMENTS_API_KEY"] = ""
    try:
        _assert(not np.is_enabled(), "no key → feature off")
        raised = False
        try:
            np._request("GET", "status")
        except np.NowPaymentsError as exc:
            raised = exc.code == "no_key"
        _assert(raised, "no key → explicit error, never a silent call")
    finally:
        os.environ["NOWPAYMENTS_API_KEY"] = "test-key"


def test_create_payment_parses_address_amount_and_expiry() -> None:
    transport = _install(FakeTransport(FakeResponse(201, PAYMENT_BODY)))
    try:
        p = np.create_payment(
            price_amount=Decimal("10"), order_id="u1-pro-abc", order_description="Pro"
        )
    finally:
        _restore()
    _assert(p.payment_id == "5745459419", p.payment_id)
    _assert(p.pay_address.startswith("0x") and len(p.pay_address) == 42, p.pay_address)
    _assert(p.pay_amount == "10.041234", "exact crypto amount kept as a string")
    _assert(p.payment_status == "waiting", p.payment_status)
    _assert(p.valid_until.startswith("2026-08-13"), p.valid_until)

    sent = transport.calls[0]
    _assert(sent["method"] == "POST", sent["method"])
    _assert(sent["url"].endswith("/payment"), sent["url"])
    _assert(sent["json"]["pay_currency"] == "usdtbsc", "pays in USDT on BSC")
    _assert(sent["json"]["price_currency"] == "usd", "priced in USD")
    _assert(sent["json"]["order_id"] == "u1-pro-abc", "order id round-trips")
    _assert(sent["headers"]["x-api-key"] == "test-key", "key sent per request")


def test_create_payment_price_serializes_exactly() -> None:
    """Money must not drift through float repr on the wire (AGENTS 6)."""
    transport = _install(FakeTransport(FakeResponse(201, PAYMENT_BODY)))
    try:
        np.create_payment(
            price_amount=Decimal("49.99"), order_id="u1-x", order_description="d"
        )
    finally:
        _restore()
    body = transport.calls[0]["json"]
    _assert(body["price_amount"] == 49.99, f"exact cents on the wire: {body}")


def test_create_payment_without_address_is_an_error() -> None:
    body = dict(PAYMENT_BODY)
    body["pay_address"] = ""
    _install(FakeTransport(FakeResponse(201, body)))
    try:
        np.create_payment(price_amount="2", order_id="x", order_description="y")
    except np.NowPaymentsError as exc:
        _assert(exc.code == "bad_body", exc.code)
    else:
        raise AssertionError("missing address must not pass as a payment")
    finally:
        _restore()


def test_get_payment_normalizes_status_and_expiry_alias() -> None:
    body = dict(PAYMENT_BODY)
    body["payment_status"] = "PARTIALLY_PAID"
    body["actually_paid"] = "9.5"
    body.pop("valid_until")
    body["expiration_estimate_date"] = "2026-08-14T00:00:00.000Z"
    _install(FakeTransport(FakeResponse(200, body)))
    try:
        p = np.get_payment("5745459419")
    finally:
        _restore()
    _assert(p.payment_status == "partially_paid", p.payment_status)
    _assert(p.actually_paid == "9.5", p.actually_paid)
    _assert(p.valid_until.startswith("2026-08-14"), "falls back to the estimate field")


def test_status_helpers() -> None:
    _assert(np.is_terminal("finished"), "finished is terminal")
    _assert(np.is_terminal("expired"), "expired is terminal")
    _assert(not np.is_terminal("waiting"), "waiting is not")
    _assert(not np.is_terminal("partially_paid"), "underpaid can still complete")
    _assert(not np.is_terminal("something_new"), "unknown status is not terminal")


def test_retry_on_429_then_success() -> None:
    transport = _install(
        FakeTransport(
            FakeResponse(429, None, text="rate limited", headers={"Retry-After": "0"}),
            FakeResponse(200, {"message": "OK"}),
        )
    )
    try:
        ok, _ = np.api_status()
    finally:
        _restore()
    # api_status uses retries=1, so a 429 there is fatal — check _request directly.
    transport = _install(
        FakeTransport(
            FakeResponse(429, None, text="rate limited", headers={"Retry-After": "0"}),
            FakeResponse(200, {"min_amount": 1.4}),
        )
    )
    try:
        data = np._request("GET", "min-amount", retries=3)
    finally:
        _restore()
    _assert(data["min_amount"] == 1.4, "second attempt wins")
    _assert(len(transport.calls) == 2, f"exactly one retry: {len(transport.calls)}")


def test_no_retry_on_400() -> None:
    transport = _install(
        FakeTransport(
            FakeResponse(400, None, text='{"message":"bad currency"}'),
            FakeResponse(200, {"ok": True}),
        )
    )
    try:
        np._request("GET", "min-amount", retries=3)
    except np.NowPaymentsError as exc:
        _assert(exc.http_status == 400, "400 surfaces")
        _assert(not exc.retryable, "a client error is permanent")
        _assert("bad currency" in exc.detail, "raw body kept for the operator")
    else:
        raise AssertionError("400 must raise")
    finally:
        _restore()
    _assert(len(transport.calls) == 1, "no retry on a 4xx")


def test_post_payment_is_not_retried_on_read_timeout() -> None:
    transport = _install(
        FakeTransport(
            requests.exceptions.ReadTimeout("timeout"),
            FakeResponse(201, PAYMENT_BODY),
        )
    )
    try:
        np.create_payment(price_amount="2", order_id="dup-risk", order_description="x")
    except np.NowPaymentsError as exc:
        _assert(exc.code == "read_timeout", exc.code)
        _assert(not exc.retryable, "caller must use a fresh order_id, not a blind retry")
    else:
        raise AssertionError("read timeout on POST must raise")
    finally:
        _restore()
    _assert(len(transport.calls) == 1, "must not fire a second create")


def test_get_is_retried_on_connection_error() -> None:
    transport = _install(
        FakeTransport(
            requests.exceptions.ConnectionError("boom"),
            FakeResponse(200, {"payment_id": "1", "payment_status": "waiting"}),
        )
    )
    try:
        p = np.get_payment("1")
    finally:
        _restore()
    _assert(p.payment_id == "1", "reads are safe to retry")
    _assert(len(transport.calls) == 2, f"one retry: {len(transport.calls)}")


def test_min_amount_prefers_fiat_and_caches() -> None:
    _install(FakeTransport(FakeResponse(200, {"min_amount": 1.4, "fiat_equivalent": 1.42})))
    try:
        value = np.refresh_min_amount_cache(force=True)
    finally:
        _restore()
    _assert(value == Decimal("1.42"), f"fiat equivalent used: {value}")
    _assert(billing.min_amount_usd() == Decimal("1.42"), "cached in bot_settings")
    _assert(not billing.min_amount_is_stale(), "freshly checked")
    _assert(billing.is_purchasable("basic"), "$50 clears a $1.42 minimum")

    _install(FakeTransport(FakeResponse(200, {"min_amount": 60.0})))
    try:
        np.refresh_min_amount_cache(force=True)
    finally:
        _restore()
    # With one plan there is no cheaper tier to fall back to: a provider
    # minimum above the only price shuts the shop by arithmetic.
    _assert(not billing.is_purchasable("basic"), "$50 below a $60 minimum")
    _assert([p.id for p in billing.unpurchasable_plans()] == ["basic"], "and it is flagged")


def test_min_amount_failure_keeps_the_previous_value() -> None:
    bot_db.set_setting(billing.SETTING_MIN_AMOUNT, "1.5")
    _install(FakeTransport(FakeResponse(500, None, text="server error")))
    try:
        value = np.refresh_min_amount_cache(force=True)
    finally:
        _restore()
    _assert(value == Decimal("1.5"), f"degrade to the cached value: {value}")


def test_error_detail_never_reaches_the_user() -> None:
    from bot import errors as bot_errors

    _install(FakeTransport(FakeResponse(400, None, text='{"message":"invalid api key"}')))
    try:
        np._request("GET", "status", retries=1)
    except np.NowPaymentsError as exc:
        user_text = bot_errors.user_message("FAIL", str(exc), lang="ru")
        _assert("invalid api key" not in user_text, user_text)
        _assert("{" not in user_text, user_text)
    finally:
        _restore()


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
    _restore()
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
