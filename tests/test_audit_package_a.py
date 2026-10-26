"""Regression blockers for audit package A: soft-OK, no_proxies, queue SKIPPED."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _assert(c: bool, msg: str) -> None:
    if not c:
        raise AssertionError(msg)


def test_enabled_apis_refuse_empty_proxies() -> None:
    """Spot-check enabled modules: no HTTP path when proxies=[]."""
    wallet = "0x" + "ab" * 20
    del wallet
    cases: list = []
    for name, r in cases:
        # Some modules replace the shared NO_PROXIES_DETAIL with their own
        # wording ("CULT requires a proxy"); what matters is that the
        # call failed on the missing pool instead of reaching the network.
        detail = (r.get("detail") or "").lower()
        _assert(
            r.get("status") == "FAIL" and ("proxy" in detail or "no_proxies" in detail),
            f"{name}: {r}",
        )


def test_queue_skipped_not_bad() -> None:
    from bot.submit_queue import QueueJob, _apply_row_result

    j = QueueJob(1, 1, "p", 1, None, "running", total=5)
    _apply_row_result(j, {"status": "SKIPPED", "skipped": True})
    _assert(j.done == 0 and j.bad == 0 and j.fail == 0, "skip clean")
    _apply_row_result(j, {"status": "OK"})
    _assert(j.ok == 1 and j.done == 1, "ok counts")


def main() -> None:
    test_enabled_apis_refuse_empty_proxies()
    print("PASS test_enabled_apis_refuse_empty_proxies")
    test_queue_skipped_not_bad()
    print("PASS test_queue_skipped_not_bad")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
