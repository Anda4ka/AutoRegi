"""Reading a project's X timeline, paying by the post.

X is pay-per-use now — no contract, no subscription, no minimum. The prices
that matter here, from `console.x.com/api/credits/pricing` on 2026-08-17:

    User  $0.01   one lookup per handle, ever
    Post  $0.005  each post returned

Which makes the whole cost model about **not asking twice**. A handle's numeric
id never changes, so it is looked up once and kept. And every timeline read
passes `since_id`, so a check that finds nothing new costs nothing at all —
which means the polling interval is free to be short. Four checks a day and
twenty-four cost the same; only new posts are billed.

Retweets are kept. One project's supply and price were found only in a retweeted
giveaway post, not in anything the project wrote itself.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from bot import db as bot_db

API = "https://api.x.com/2"
TIMEOUT = 30
_SETTING_UID = "x_user_id."
_SETTING_SINCE = "x_since_id."


def bearer() -> str:
    return os.getenv("X_BEARER_TOKEN", "").strip()


def enabled() -> bool:
    return bool(bearer())


class XError(Exception):
    """A refusal from X worth showing an operator; never carries the token."""


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer()}", "User-Agent": "autoreg-mints/1"}


def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    if not enabled():
        raise XError("X_BEARER_TOKEN не задан")
    for attempt in range(3):
        try:
            r = requests.get(f"{API}{path}", headers=_headers(),
                             params=params or {}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 2:
                raise XError(f"сеть: {type(exc).__name__}") from None
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            # Rate limited. Sleeping the full reset window would block a
            # scanner that has 17 more projects to visit, so this gives up and
            # lets the caller try the project again next round.
            raise XError("429: лимит запросов X")
        if r.status_code in (401, 403):
            raise XError(f"{r.status_code}: токен не принят или нет доступа")
        if r.status_code >= 500:
            if attempt == 2:
                raise XError(f"{r.status_code}: X недоступен")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise XError(f"{r.status_code}: {r.text[:120]}")
        try:
            return r.json()
        except ValueError:
            raise XError("ответ X не разобрался") from None
    raise XError("не удалось получить ответ")


def handle_from_url(url: str) -> str:
    """`https://x.com/Foo?lang=en` -> `Foo`. Empty when there is no handle."""
    raw = (url or "").strip()
    if not raw:
        return ""
    raw = raw.split("?")[0].rstrip("/")
    for prefix in ("https://x.com/", "http://x.com/", "https://twitter.com/",
                   "http://twitter.com/", "x.com/", "twitter.com/", "@"):
        if raw.lower().startswith(prefix.lower()):
            raw = raw[len(prefix):]
            break
    parts = [x for x in raw.split("/") if x]
    handle = parts[0].strip() if parts else ""
    # `x.com/i/status/123` is a link to a post, not to an account, and reading
    # it as the handle `i` would send every lookup to the wrong place. Same for
    # the other paths X reserves.
    if len(parts) > 1 or handle.lower() in (
        "i", "home", "search", "explore", "notifications", "messages",
        "settings", "intent", "share", "hashtag", "compose",
    ):
        return ""
    return handle if handle and handle.replace("_", "").isalnum() else ""


def user_id(handle: str) -> str:
    """Numeric id for a handle, looked up once and remembered.

    Costs $0.01 the first time and nothing afterwards. A handle's id survives
    renames, so caching it is not a staleness risk — it is the stable half.
    """
    name = (handle or "").strip().lstrip("@")
    if not name:
        return ""
    key = _SETTING_UID + name.lower()
    cached = bot_db.get_setting(key, "")
    if cached:
        return cached
    data = _get(f"/users/by/username/{name}")
    uid = str((data.get("data") or {}).get("id") or "")
    if uid:
        bot_db.set_setting(key, uid)
    return uid


def since_id(project_id: str) -> str:
    return bot_db.get_setting(_SETTING_SINCE + project_id, "")


def remember_since(project_id: str, newest_id: str) -> None:
    if newest_id:
        bot_db.set_setting(_SETTING_SINCE + project_id, str(newest_id))


def recent_posts(
    handle: str,
    *,
    project_id: str = "",
    limit: int = 10,
    use_since: bool = True,
) -> list[dict[str, Any]]:
    """New posts from this handle, newest first.

    With `use_since` the call is bounded by what has already been seen, so a
    quiet project costs nothing to re-check. The first ever read has no marker
    and fetches `limit` posts — that is the one time a project is expensive.
    """
    uid = user_id(handle)
    if not uid:
        return []
    params: dict[str, Any] = {
        "max_results": max(5, min(int(limit), 100)),
        "tweet.fields": "created_at,text,entities",
    }
    marker = since_id(project_id) if (use_since and project_id) else ""
    if marker:
        params["since_id"] = marker
    data = _get(f"/users/{uid}/tweets", params)
    posts = data.get("data") or []
    if posts and project_id:
        remember_since(project_id, str(posts[0].get("id") or ""))
    out = []
    for post in posts:
        out.append({
            "id": str(post.get("id") or ""),
            "text": str(post.get("text") or ""),
            "created_at": str(post.get("created_at") or ""),
            "url": f"https://x.com/{handle.lstrip('@')}/status/{post.get('id')}",
        })
    return out
