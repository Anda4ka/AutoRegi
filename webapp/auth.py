"""Telegram Mini App `initData` verification.

This is the whole security boundary of the web app. The page runs in the
user's Telegram client and sends `window.Telegram.WebApp.initData` with every
request; that string is the *only* thing proving who is asking. A user id taken
from a query parameter would let anyone read anyone's wallets, so nothing here
ever reads one.

The check is Telegram's published scheme:

    secret_key    = HMAC_SHA256(key=b"WebAppData", msg=bot_token)
    data_check    = "\\n".join(f"{k}={v}" for k, v in sorted(pairs) if k != "hash")
    expected_hash = HMAC_SHA256(key=secret_key, msg=data_check).hexdigest()

and it is only half the job — a valid signature stays valid forever, so a
leaked `initData` would be a permanent key. `MAX_AGE_S` bounds that: an
`auth_date` older than the window is refused even when the hash is perfect.

Comparisons use `hmac.compare_digest`, and a failure never says which part was
wrong — the caller gets one flat "bad initData".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# A signature does not expire on its own; this is what keeps a captured
# initData from being a permanent credential. Telegram refreshes it whenever
# the mini app is opened, so a day is generous for a session.
MAX_AGE_S = 24 * 3600


class AuthError(Exception):
    """Raised for anything wrong with initData. Deliberately uninformative."""


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def verify_init_data(
    init_data: str, bot_token: str, *, max_age_s: int = MAX_AGE_S, now: float | None = None
) -> dict:
    """Return the parsed, verified fields — or raise `AuthError`.

    The returned dict has `user` already decoded from its JSON payload.
    """
    if not init_data or not bot_token:
        raise AuthError("bad initData")

    # `parse_qsl` with keep_blank_values keeps the pairs Telegram signed; a
    # dropped empty value would change the data-check string and fail the hash.
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise AuthError("bad initData")

    data = dict(pairs)
    received_hash = data.pop("hash", "")
    # compare_digest raises TypeError on non-ASCII strings, and that would
    # escape _authenticate as something other than AuthError — a garbage hash
    # must fail the same flat 401 as every other bad credential, not drop the
    # connection with a traceback.
    if not received_hash or not received_hash.isascii():
        raise AuthError("bad initData")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    expected = hmac.new(
        _secret_key(bot_token), check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("bad initData")

    try:
        auth_date = int(data.get("auth_date") or 0)
    except (TypeError, ValueError):
        raise AuthError("bad initData") from None
    current = time.time() if now is None else now
    if (
        auth_date <= 0
        or current - auth_date > max_age_s
        or auth_date - current > 300
    ):
        # A signature stays valid forever; freshness is what bounds a leak.
        # A future auth_date only gets clock-skew tolerance, not an unbounded
        # window a backdated-then-forwarded capture could ride.
        raise AuthError("bad initData")

    raw_user = data.get("user") or ""
    try:
        user = json.loads(raw_user) if raw_user else {}
    except (TypeError, ValueError):
        raise AuthError("bad initData") from None
    if not isinstance(user, dict) or not user.get("id"):
        raise AuthError("bad initData")

    data["user"] = user
    return data


def user_id_from(init_data: str, bot_token: str, **kwargs) -> int:
    """The only sanctioned way to learn who is asking."""
    return int(verify_init_data(init_data, bot_token, **kwargs)["user"]["id"])


def user_from(init_data: str, bot_token: str, **kwargs) -> dict:
    """Verified id plus the display fields, for the user row the bot keeps.

    Every chat handler starts with `ensure_user`; the web had no equivalent, so
    a person who opened the app without ever pressing /start had no row — and
    the first write with a foreign key onto `users` failed. Telegram already
    signs the name alongside the id, so there is nothing extra to fetch.
    """
    data = verify_init_data(init_data, bot_token, **kwargs)["user"]
    return {
        "id": int(data["id"]),
        "username": str(data.get("username") or "")[:64],
        "first_name": str(data.get("first_name") or "")[:64],
    }
