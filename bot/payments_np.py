"""NOWPayments HTTP client — USDT on BNB Smart Chain (BEP-20).

Only three calls matter for this bot: create a payment, read its status, and
ask what the minimum payable amount is. Everything else NOWPayments offers
(invoices, payouts, recurring plans) either needs a hosted checkout, an IPN
endpoint or a JWT login, and this host has no public HTTPS endpoint — the
payment address goes straight into the chat and a poller reads the status.

Raw response bodies live in ``NowPaymentsError.detail`` and the log; they are
never shown to a user (see bot/errors.py).
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import requests

from bot import billing
from bot import config
from bot import db as bot_db


PROD_BASE = "https://api.nowpayments.io/v1"
SANDBOX_BASE = "https://api-sandbox.nowpayments.io/v1"

# Statuses NOWPayments can report for a payment.
WAITING = frozenset({"waiting", "confirming", "confirmed", "sending"})
PARTIAL = frozenset({"partially_paid"})
TERMINAL_OK = frozenset({"finished"})
TERMINAL_BAD = frozenset({"failed", "refunded", "expired"})

CONNECT_TIMEOUT = 5.0
RETRY_STATUS = (429, 500, 502, 503, 504)


class NowPaymentsError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        http_status: int | None = None,
        detail: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.detail = detail  # raw body — logs/admin only
        self.retryable = retryable


@dataclass(frozen=True)
class NPPayment:
    payment_id: str
    order_id: str
    pay_address: str
    pay_amount: str
    pay_currency: str
    price_amount: str
    payment_status: str
    valid_until: str = ""
    actually_paid: str = ""
    raw: dict = field(default_factory=dict, repr=False)


def api_base() -> str:
    explicit = str(getattr(config, "NOWPAYMENTS_BASE_URL", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("NOWPAYMENTS_BASE_URL", "").strip():
        return os.environ["NOWPAYMENTS_BASE_URL"].strip().rstrip("/")
    sandbox = os.environ.get("NOWPAYMENTS_SANDBOX", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ) or bool(getattr(config, "NOWPAYMENTS_SANDBOX", False))
    return SANDBOX_BASE if sandbox else PROD_BASE


def api_key() -> str:
    # Read from the environment on every call so /reload and tests can change
    # it, and so an empty key simply switches the paid UI off.
    return os.environ.get("NOWPAYMENTS_API_KEY", "").strip()


def is_enabled() -> bool:
    return bool(api_key())


def pay_currency() -> str:
    return str(getattr(config, "NOWPAYMENTS_PAY_CURRENCY", "usdtbsc") or "usdtbsc")


def _headers() -> dict[str, str]:
    return {
        "x-api-key": api_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _read_timeout() -> float:
    return float(getattr(config, "NOWPAYMENTS_HTTP_TIMEOUT", 20) or 20)


def _sleep_for(attempt: int, response=None) -> float:
    if response is not None:
        raw = (response.headers or {}).get("Retry-After", "")
        try:
            return max(0.5, min(30.0, float(raw)))
        except (TypeError, ValueError):
            pass
    return (2**attempt) + random.uniform(0, 0.5)


def _request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    retries: int = 3,
    idempotent: bool = True,
) -> dict:
    """One API call with bounded retries. Never retries a 4xx except 429.

    ``idempotent=False`` also disables the retry after a read timeout: the
    request may already have created a payment server-side, and a blind repeat
    would leave a second one behind.
    """
    if not api_key():
        raise NowPaymentsError("NOWPayments API key is not configured", code="no_key")
    url = f"{api_base()}/{path.lstrip('/')}"
    attempts = max(1, int(retries))
    last: Exception | None = None

    for attempt in range(attempts):
        try:
            response = requests.request(
                method,
                url,
                headers=_headers(),
                json=json_body,
                params=params,
                timeout=(CONNECT_TIMEOUT, _read_timeout()),
            )
        except requests.exceptions.ReadTimeout as exc:
            last = exc
            if not idempotent:
                raise NowPaymentsError(
                    "NOWPayments did not answer in time", code="read_timeout"
                ) from exc
            if attempt + 1 < attempts:
                time.sleep(_sleep_for(attempt))
                continue
            raise NowPaymentsError(
                "NOWPayments did not answer in time", code="read_timeout", retryable=True
            ) from exc
        except requests.exceptions.RequestException as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(_sleep_for(attempt))
                continue
            raise NowPaymentsError(
                "Could not reach NOWPayments", code="network", retryable=True
            ) from exc

        body = (response.text or "")[:1000]
        if response.status_code in RETRY_STATUS and attempt + 1 < attempts:
            print(f"[np] {method} {path} -> {response.status_code} (retry)", flush=True)
            time.sleep(_sleep_for(attempt, response))
            continue
        if response.status_code >= 400:
            print(f"[np] {method} {path} -> {response.status_code} {body[:300]}", flush=True)
            raise NowPaymentsError(
                f"NOWPayments answered {response.status_code}",
                code="http_error",
                http_status=response.status_code,
                detail=body,
                retryable=response.status_code in RETRY_STATUS,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise NowPaymentsError(
                "NOWPayments returned a non-JSON body", code="bad_body", detail=body
            ) from exc
        if not isinstance(data, dict):
            raise NowPaymentsError(
                "NOWPayments returned an unexpected body", code="bad_body", detail=body
            )
        return data

    raise NowPaymentsError(
        "NOWPayments request failed", code="exhausted", retryable=True, detail=str(last)
    )


def api_status() -> tuple[bool, str]:
    """Reachability probe for the admin panel."""
    try:
        data = _request("GET", "status", retries=1)
    except NowPaymentsError as exc:
        return False, str(exc)
    return str(data.get("message", "")).lower() == "ok", str(data.get("message", ""))


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def min_amount(
    currency_from: str | None = None,
    currency_to: str | None = None,
    *,
    fiat_equivalent: str = "usd",
) -> Decimal | None:
    """Minimum payable amount, expressed in ``fiat_equivalent`` when possible."""
    coin = currency_from or pay_currency()
    params = {
        "currency_from": coin,
        "currency_to": currency_to or coin,
        "fiat_equivalent": fiat_equivalent,
    }
    data = _request("GET", "min-amount", params=params, retries=2)
    fiat = _decimal_or_none(data.get("fiat_equivalent"))
    if fiat is not None and fiat > 0:
        return fiat
    # No fiat equivalent: for a USD stablecoin the coin amount is close enough.
    return _decimal_or_none(data.get("min_amount"))


def refresh_min_amount_cache(*, force: bool = False) -> Decimal | None:
    """Store the minimum in bot_settings; billing reads it from there."""
    if not force and not billing.min_amount_is_stale():
        return billing.min_amount_usd()
    try:
        value = min_amount()
    except NowPaymentsError as exc:
        print(f"[np] min-amount check failed: {exc}", flush=True)
        return billing.min_amount_usd()
    if value is None:
        return billing.min_amount_usd()
    bot_db.set_setting(billing.SETTING_MIN_AMOUNT, str(value))
    bot_db.set_setting(
        billing.SETTING_MIN_CHECKED,
        bot_db._now(),  # noqa: SLF001 - same module family, single timestamp source
    )
    return value


def _as_payment(data: dict) -> NPPayment:
    return NPPayment(
        payment_id=str(data.get("payment_id") or data.get("id") or ""),
        order_id=str(data.get("order_id") or ""),
        pay_address=str(data.get("pay_address") or ""),
        pay_amount=str(data.get("pay_amount") or ""),
        pay_currency=str(data.get("pay_currency") or ""),
        price_amount=str(data.get("price_amount") or ""),
        payment_status=normalize_status(str(data.get("payment_status") or "")),
        valid_until=str(
            data.get("valid_until")
            or data.get("expiration_estimate_date")
            or data.get("expiration_date")
            or ""
        ),
        actually_paid=str(data.get("actually_paid") or ""),
        raw=data,
    )


def create_payment(
    *,
    price_amount: Decimal | str,
    order_id: str,
    order_description: str,
    pay_currency_code: str | None = None,
    price_currency: str = "usd",
) -> NPPayment:
    """Create one payment and return its address and exact amount."""
    # Money must never cross the wire as an arbitrary binary float: quantize
    # to the USD granularity first — a 2-dp float reprs exactly in JSON
    # (49.99 -> "49.99"), an unquantized one can drift by a few wei.
    price_dec = Decimal(str(price_amount)).quantize(Decimal("0.01"))
    body = {
        "price_amount": float(price_dec),
        "price_currency": price_currency,
        "pay_currency": pay_currency_code or pay_currency(),
        "order_id": order_id,
        "order_description": order_description[:255],
    }
    # Not idempotent: a read timeout may still have created the payment, so a
    # retry would orphan one. The caller retries with a fresh order_id instead.
    data = _request("POST", "payment", json_body=body, idempotent=False)
    payment = _as_payment(data)
    if not payment.payment_id or not payment.pay_address:
        raise NowPaymentsError(
            "NOWPayments did not return a payment address",
            code="bad_body",
            detail=str(data)[:500],
        )
    return payment


def get_payment(payment_id: str) -> NPPayment:
    data = _request("GET", f"payment/{payment_id}")
    return _as_payment(data)


def normalize_status(status: str) -> str:
    return str(status or "").strip().lower()


def is_terminal(status: str) -> bool:
    s = normalize_status(status)
    return s in TERMINAL_OK or s in TERMINAL_BAD
