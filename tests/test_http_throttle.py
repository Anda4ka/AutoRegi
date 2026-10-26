"""Tests for global HTTP throttling primitives."""

from __future__ import annotations

from bot.http_throttle import SlidingWindowLimiter


def test_sliding_window_limiter_records_admissions() -> None:
    limiter = SlidingWindowLimiter(2, window_seconds=60)
    limiter.acquire()
    limiter.acquire()
    assert len(limiter._events) == 2
