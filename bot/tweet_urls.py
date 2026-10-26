"""Synthetic X/Twitter status URLs for projects that need RT/reply/quote links."""

from __future__ import annotations

import random
import re
import time

from bot.http_client import normalize_twitter

# Twitter snowflake epoch (ms) — same as real X IDs.
TWITTER_EPOCH_MS = 1_288_834_974_657
# Real X handle limit.
HANDLE_MAX = 15
# How far back a generated id may point. A project reviews the entries it just
# collected, so a proof link older than the registration itself is the odd one
# out — 12 hours keeps every generated post inside the window a reviewer would
# consider "during the campaign".
MAX_AGE_HOURS = 12
MAX_AGE_MS = MAX_AGE_HOURS * 60 * 60 * 1000

# Keys in bulk_columns that mean "tweet/status URL field"
_URL_KEY_HINTS = (
    "url",
    "tweet",
    "comment",
    "post",
    "retweet",
    "repost",
    "quote",
    "rt_tweet",
    "rt_tweet_url",
    "comment_url",
    "post_link",
    "retweet_url",
    "repost_url",
    "quote_url",
)


def is_url_field_key(key: str) -> bool:
    k = (key or "").lower().strip()
    if not k:
        return False
    if k in _URL_KEY_HINTS:
        return True
    return any(
        h in k for h in ("url", "tweet", "comment", "post", "retweet", "repost", "quote")
    )


def count_url_fields(bulk_columns: list | None) -> int:
    n = 0
    for col in bulk_columns or []:
        if isinstance(col, (list, tuple)) and col:
            key = str(col[0])
        elif isinstance(col, dict):
            key = str(col.get("key") or col.get("name") or "")
        else:
            continue
        if is_url_field_key(key):
            n += 1
    return n


def _clean_handle(handle: str) -> str:
    """Bare handle for URL path: X limit 15, [A-Za-z0-9_]."""
    h = normalize_twitter(handle, max_len=HANDLE_MAX)
    if h:
        return h
    # Fallback: strip junk and truncate (synthetic URLs only)
    raw = re.sub(r"[^A-Za-z0-9_]", "", (handle or "").strip().lstrip("@"))
    return (raw[:HANDLE_MAX] if raw else "") or "user"


def snowflake_ms(status_id: int | str) -> int:
    """Wall-clock ms encoded in a snowflake id (0 when it is not one)."""
    try:
        value = int(status_id)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return (value >> 22) + TWITTER_EPOCH_MS


def generate_snowflake_like(
    *, rng: random.Random | None = None, after_id: int | str = 0
) -> int:
    """
    Fake Twitter snowflake: (ms since epoch << 22) | worker | sequence.

    Decodes to a plausible recent date; always 18–19 digits (today almost always 19).

    ``after_id`` raises the floor to just after another post. A reply that
    decodes to *before* the post it answers is the giveaway a reviewer looks
    for, so projects that verify "reply to this tweet" pass the target id here
    and get a timestamp between that post and now.
    """
    r = rng or random
    now_ms = int(time.time() * 1000)
    # Timestamp within the last MAX_AGE_HOURS (a post from during the campaign)
    low_ms = now_ms - MAX_AGE_MS
    after_ms = snowflake_ms(after_id)
    if after_ms:
        # At least a second later — same-millisecond replies do not happen.
        low_ms = max(low_ms, after_ms + 1000)
    if low_ms >= now_ms:
        # Reference post is newer than the window (or than our clock): the only
        # honest answer is "just now" rather than a timestamp in the future.
        low_ms = now_ms
    ts = r.randint(low_ms, now_ms) - TWITTER_EPOCH_MS
    if ts < 1:
        ts = r.randint(1, 10**12)
    worker = r.randint(0, 1023)  # 10 bits
    seq = r.randint(0, 4095)  # 12 bits
    return (int(ts) << 22) | (worker << 12) | seq


def generate_status_url(
    handle: str, *, rng: random.Random | None = None, after_id: int | str = 0
) -> str:
    """https://x.com/{handle}/status/{snowflake-like id}."""
    r = rng or random
    h = _clean_handle(handle)
    sid = generate_snowflake_like(rng=r, after_id=after_id)
    return f"https://x.com/{h}/status/{sid}"


def generate_status_urls(
    handle: str,
    count: int,
    *,
    rng: random.Random | None = None,
    after_id: int | str = 0,
) -> list[str]:
    count = max(0, int(count))
    r = rng or random
    # Avoid rare ID collisions within one multi-URL row
    seen: set[int] = set()
    out: list[str] = []
    h = _clean_handle(handle)
    for _ in range(count):
        for _try in range(8):
            sid = generate_snowflake_like(rng=r, after_id=after_id)
            if sid not in seen:
                seen.add(sid)
                break
        out.append(f"https://x.com/{h}/status/{sid}")
    return out
