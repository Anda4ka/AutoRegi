"""Exits that keep failing at the transport layer stop getting rows.

`bot/exit_budget.py` retires an exit that answers 429 — that is a route
refusing us, and it is opt-in per project. This is the other failure: the exit
itself is broken, the proxy refuses or the TLS handshake dies, and it hurts
every project the same way. Nothing was counting those. Measured over five days
of journal, one gateway produced 339 `ProxyError` and 236 `SSLError` — 44% of
every network error the bot logged — and stayed in rotation the whole time.

Two properties matter and pull against each other: a bad exit must leave the
rotation, and a bad *gateway* must not empty the pool. The last test here is
the second one.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from requests.exceptions import ProxyError, SSLError  # noqa: E402

from bot import http_client as hc  # noqa: E402

# Never touch the production bench ledger from tests.
hc._BENCH_PERSIST = False

POOL = [f"http://u{i}:p@host{i}.example.com:8080" for i in range(6)]
BAD = POOL[2]


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _fail(url: str, times: int) -> None:
    for _ in range(times):
        hc.note_transport_failure(url)


def test_one_bad_answer_is_not_enough_to_bench() -> None:
    """Exits blip. Benching on a single failure would empty a good pool."""
    hc.reset_proxy_health()
    _fail(BAD, hc._PROXY_FAIL_MAX - 1)
    _ok(not hc.proxy_is_benched(BAD), "benched too eagerly")


def test_repeated_failures_take_the_exit_out_of_rotation() -> None:
    hc.reset_proxy_health()
    _fail(BAD, hc._PROXY_FAIL_MAX)
    _ok(hc.proxy_is_benched(BAD), "a repeatedly failing exit stayed in rotation")
    picked = {hc.choose_proxy_url(POOL, i, 1) for i in range(len(POOL) * 3)}
    _ok(BAD not in picked, f"the benched exit was handed out again: {picked}")
    _ok(len(picked) >= 2, "the pool collapsed onto one exit")


def test_the_bench_expires() -> None:
    """A cooldown, not a blacklist: exits come back."""
    hc.reset_proxy_health()
    _fail(BAD, hc._PROXY_FAIL_MAX)
    _ok(hc.proxy_is_benched(BAD))
    later = _wall_now() + hc._PROXY_BENCH_S + 1
    _ok(not hc.proxy_is_benched(BAD, later), "the bench never lifted")


def test_repeat_offences_escalate() -> None:
    """Each bench of the same exit doubles its TTL, capped at a day.

    A flat 15-minute bench let a chronically dead exit return every quarter
    hour forever; over 7 days of submissions 405 distinct exits collected
    723 transport failures with no single villain — the leverage is in how
    long the repeat offenders stay out, not in naming them.
    """
    hc.reset_proxy_health()
    key = hc._bench_key(BAD)
    _fail(BAD, hc._PROXY_FAIL_MAX)
    _ok(hc._proxy_bench[key][1] == 1, "first bench should be strike 1")
    first_ttl = min(hc._PROXY_BENCH_S, hc._PROXY_BENCH_MAX_S)
    _ok(hc.proxy_is_benched(BAD, _wall_now() + first_ttl - 5))
    _ok(not hc.proxy_is_benched(BAD, _wall_now() + first_ttl + 5))
    # The strike is remembered past the TTL, so the next bench escalates.
    _fail(BAD, hc._PROXY_FAIL_MAX)
    _ok(hc._proxy_bench[key][1] == 2, "second bench should be strike 2")
    second_ttl = min(hc._PROXY_BENCH_S * 2, hc._PROXY_BENCH_MAX_S)
    _ok(hc.proxy_is_benched(BAD, _wall_now() + first_ttl + 5),
        "strike 2 should still be benched past strike 1's TTL")
    _ok(not hc.proxy_is_benched(BAD, _wall_now() + second_ttl + 5))
    # Heavy repeaters max out at a day.
    for _ in range(10):
        _fail(BAD, hc._PROXY_FAIL_MAX)
    _ok(hc.proxy_is_benched(BAD, _wall_now() + hc._PROXY_BENCH_MAX_S - 5),
        "a chronic offender should stay benched for the capped day")
    _ok(not hc.proxy_is_benched(BAD, _wall_now() + hc._PROXY_BENCH_MAX_S + 5))


def test_the_bench_survives_a_restart_via_the_mirror() -> None:
    """The ledger is mirrored to STATE_DIR so a restart cannot forget it.

    Same rule as exit_budget: hashed keys, no credentials in the file, and a
    restart that hands dead exits new rows is an outage the operator already
    paid for once (2026-09-17: FAIL×43 alert right after a restart).
    """
    import json
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp:
        mirror = os.path.join(tmp, "proxy_bench.json")
        hc._BENCH_PERSIST = True
        hc._BENCH_PATH_OVERRIDE = mirror
        try:
            hc.reset_proxy_health()
            _fail(BAD, hc._PROXY_FAIL_MAX)
            _ok(hc.proxy_is_benched(BAD))
            with open(mirror, encoding="utf-8") as fh:
                raw = json.load(fh)
            _ok(hc._bench_key(BAD) in raw, "the bench was not mirrored")
            # Simulate the restart: fresh process state, same file.
            hc._proxy_bench.clear()
            hc._bench_loaded = False
            _ok(hc.proxy_is_benched(BAD),
                "a restarted process forgot the persisted bench")
        finally:
            hc._BENCH_PERSIST = False
            hc._BENCH_PATH_OVERRIDE = None


def test_failures_age_out_so_a_slow_trickle_never_benches() -> None:
    """No success hook exists, so the counter has to forget on its own.

    Counting cumulatively instead of in a window would bench every exit in the
    pool given enough hours, which is the same outage by a slower route.
    """
    hc.reset_proxy_health()
    url = POOL[4]
    hits = hc._proxy_fails.setdefault(url, hc.deque())
    # Two failures, long ago.
    hits.append(_wall_now() - hc._PROXY_FAIL_WINDOW_S - 60)
    hits.append(_wall_now() - hc._PROXY_FAIL_WINDOW_S - 30)
    hc.note_transport_failure(url)  # one fresh failure
    _ok(not hc.proxy_is_benched(url),
        "stale failures were counted towards the bench")


def test_a_dead_gateway_does_not_empty_the_pool() -> None:
    """When every exit is benched, hand one out anyway.

    The pool here is one rotating gateway whose lines are distinct exits. If it
    goes down as a whole, every line fails and every line gets benched — and
    returning nothing would turn a retryable transport error into "no proxies",
    which the queue treats as a refusal rather than something to retry.
    """
    hc.reset_proxy_health()
    for url in POOL:
        _fail(url, hc._PROXY_FAIL_MAX)
    _ok(all(hc.proxy_is_benched(u) for u in POOL), "setup failed")
    chosen = hc.choose_proxy_url(POOL, 3, 1)
    _ok(chosen in POOL, f"an all-benched pool returned {chosen!r}")


def test_a_transport_error_reaches_the_ledger_through_the_formatter() -> None:
    """`format_request_error` is the hook every adapter already calls."""
    hc.reset_proxy_health()
    for exc in (ProxyError("tunnel refused"),
                SSLError("handshake failure"),
                ProxyError("timed out")):
        text = hc.format_request_error(exc, BAD)
        _ok(isinstance(text, str) and text, "the formatter stopped describing")
    _ok(hc.proxy_is_benched(BAD),
        "three transport errors through the real call path did not bench")


def test_an_application_error_does_not_bench_the_exit() -> None:
    """A 400 from the site says nothing about the proxy that carried it."""
    hc.reset_proxy_health()
    for _ in range(6):
        hc.format_request_error(ValueError("bad json in response"), BAD)
    _ok(not hc.proxy_is_benched(BAD),
        "a site-side error was blamed on the exit")


def test_direct_is_never_benched() -> None:
    hc.reset_proxy_health()
    _fail("direct", hc._PROXY_FAIL_MAX * 2)
    _ok(not hc.proxy_is_benched("direct"))


def _wall_now() -> float:
    import time

    return time.time()


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
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
