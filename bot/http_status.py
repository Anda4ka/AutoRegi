"""Shared HTTP status classification for project adapters (AUD-008).

Rules:
  - Transient 4xx: 408, 425, 429 → retry
  - Transient 5xx: 500–599 (esp. 500/502/503/504) → retry
  - Permanent 4xx: all other 4xx (404, 405, 406, 410, 422, …) → BAD_RESP
  - 409: not permanent here — adapters classify ALREADY via body first
"""

from __future__ import annotations

# Retryable client/server conditions
TRANSIENT_4XX: frozenset[int] = frozenset({408, 425, 429})
TRANSIENT_5XX_COMMON: frozenset[int] = frozenset({500, 502, 503, 504})


def is_transient_http(status_code: int | None) -> bool:
    if status_code is None:
        return False
    sc = int(status_code)
    if sc in TRANSIENT_4XX:
        return True
    if sc in TRANSIENT_5XX_COMMON or 500 <= sc <= 599:
        return True
    return False


def is_permanent_http(status_code: int | None, *, treat_409_as_permanent: bool = False) -> bool:
    """
    True if status should stop retries (business/client permanent error).

    409 defaults to False so ALREADY handlers run first.
    """
    if status_code is None:
        return False
    sc = int(status_code)
    if sc == 409 and not treat_409_as_permanent:
        return False
    if is_transient_http(sc):
        return False
    if 400 <= sc < 500:
        return True
    return False


def classify_http(status_code: int | None) -> str:
    """
    Coarse bucket for logging/tests.

    Returns: ok_range | transient | permanent_4xx | other
    """
    if status_code is None:
        return "other"
    sc = int(status_code)
    if 200 <= sc < 300:
        return "ok_range"
    if is_transient_http(sc):
        return "transient"
    if is_permanent_http(sc, treat_409_as_permanent=True):
        return "permanent_4xx"
    if sc == 409:
        return "conflict"
    return "other"
