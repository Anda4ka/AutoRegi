"""Synthetic status links (`bot/tweet_urls.py`).

The snowflake floor was written for a route whose proof link had to be
dated after the post it claimed to answer, and it was tested there. That
project was removed on 2026-08-15; the helper stayed, because every adapter
that mints a link leans on it — `felinereserve`, `golemians`,
`blastoons`, `cultnft` (the first three adapters are gone; the helper stayed).

Two properties matter and neither is obvious from the code: a floor must never
be crossed, and a floor in the future must collapse to "now" rather than mint a
link dated tomorrow.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "t:t")
os.environ.setdefault("BOT_DB", str(Path(tempfile.mkdtemp(prefix="tweeturl_")) / "t.db"))

from bot import tweet_urls  # noqa: E402

# A real X snowflake, kept as the reference floor these tests measure against.
REF = 2087915606771400873


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def test_snowflake_floor_helper() -> None:
    # Without a floor: anywhere inside the age window, never before it.
    plain = tweet_urls.generate_snowflake_like()
    _ok(plain > 0)
    # With a floor every draw is above it.
    for _ in range(200):
        sid = tweet_urls.generate_snowflake_like(after_id=REF)
        _ok(sid > REF, f"{sid} <= {REF}")
    # A floor in the future collapses to "now" instead of inventing a date.
    future = ((int(time.time() * 1000) + 86_400_000 - tweet_urls.TWITTER_EPOCH_MS) << 22)
    sid = tweet_urls.generate_snowflake_like(after_id=future)
    _ok(tweet_urls.snowflake_ms(sid) <= int(time.time() * 1000) + 5000, "no future dates")
    _ok(tweet_urls.snowflake_ms(0) == 0, "non-snowflake decodes to 0")


def test_generated_link_is_shaped_like_a_status_url() -> None:
    now_ms = int(time.time() * 1000)
    for _ in range(200):
        url = tweet_urls.generate_status_url("someHandle")
        _ok(url.startswith("https://x.com/someHandle/status/"), url)
        sid = int(url.rsplit("/", 1)[-1])
        _ok(tweet_urls.snowflake_ms(sid) <= now_ms + 5000, "link must not be in the future")


def test_generated_link_is_never_older_than_the_age_window() -> None:
    """A proof link older than the campaign is what a reviewer notices first.

    The floor is measured from the moment of generation — the same moment the
    row is submitted — so the window is «up to MAX_AGE_HOURS before this
    registration», not a calendar day.
    """
    _ok(tweet_urls.MAX_AGE_HOURS == 12, "the agreed window is 12 hours")
    for _ in range(300):
        now_ms = int(time.time() * 1000)
        sid = tweet_urls.generate_snowflake_like()
        age_ms = now_ms - tweet_urls.snowflake_ms(sid)
        _ok(-5000 <= age_ms <= tweet_urls.MAX_AGE_MS, f"age {age_ms}ms out of window")
    # Two-field rows draw from the same window.
    for url in tweet_urls.generate_status_urls("someHandle", 2):
        sid = int(url.rsplit("/", 1)[-1])
        age_ms = int(time.time() * 1000) - tweet_urls.snowflake_ms(sid)
        _ok(-5000 <= age_ms <= tweet_urls.MAX_AGE_MS, f"age {age_ms}ms out of window")

    # A floor older than the window does not widen it: an ancient reference
    # post must still yield a link from the last MAX_AGE_HOURS.
    old = ((int(time.time() * 1000) - 30 * 86_400_000 - tweet_urls.TWITTER_EPOCH_MS) << 22)
    for _ in range(50):
        sid = tweet_urls.generate_snowflake_like(after_id=old)
        age_ms = int(time.time() * 1000) - tweet_urls.snowflake_ms(sid)
        _ok(age_ms <= tweet_urls.MAX_AGE_MS, f"old floor widened the window: {age_ms}ms")


def test_distinct_links_within_one_row() -> None:
    """A form with two link fields must never receive the same URL twice."""
    first, second = tweet_urls.generate_status_urls("someHandle", 2)
    _ok(first != second, "one row's two proofs must differ")
    _ok(first.startswith("https://x.com/someHandle/status/"), first)
    _ok(second.startswith("https://x.com/someHandle/status/"), second)


def main() -> None:
    tests = [
        test_snowflake_floor_helper,
        test_generated_link_is_shaped_like_a_status_url,
        test_generated_link_is_never_older_than_the_age_window,
        test_distinct_links_within_one_row,
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
