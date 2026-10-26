"""Turning a project's posts into a proposal the operator can check.

The design exists because of one measurement, and the measurement was partly
misread — worth recording both.

On 2026-08-17, asked for mint details with no source text, a6api's grok gave
two different confident answers for the same project minutes apart. That part
stands: unstable numbers presented as fact.

OpenRouter, asked the same about a project that had announced nothing, answered twice identically —
`2026-08-20, 1999, 0.001 ETH` — citing a tweet that was not among the 43 pages
it had opened. That was called fabrication here. It was not: the operator later
produced the post, and every number and the URL were correct. What had actually
been shown was narrower — that the citation could not be verified from what the
provider fetched, which is a reason to check rather than a reason to disbelieve.

The design is unchanged, because the reason for it is unchanged: a claim whose
source cannot be checked has to be checked by a person, whether it turns out
true or invented. What changed is the confidence of the language.

So the model is never asked what it knows. It is handed one post and asked what
that post says, and it must return the sentence it read the answer in. That
quote is then checked **in code** against the post — not "does the link look
plausible", but "is this string in the text we fetched". A claim whose quote is
not there is dropped before any human sees it, because a fabrication that
reaches the confirm step has already done most of its damage: it arrives
looking like evidence.

What survives becomes a proposal. Proposals never touch the calendar.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from bot import db as bot_db
from bot import llm_client
from bot import mints as bot_mints
from bot import x_client

SYSTEM = (
    "Ты читаешь ОДИН пост из X и извлекаешь факты о минте NFT.\n"
    "Отвечай строго JSON, без пояснений:\n"
    '{"mint_date": "YYYY-MM-DD HH:MM UTC|null", "supply": число|null, '
    '"price": "строка|null", "phase": "как названо в тексте|null", '
    '"chain": "строка|null", "quote": "дословный фрагмент поста|null"}\n\n'
    "ПРАВИЛА:\n"
    "1. Только то, что написано в посте. Ничего из своих знаний.\n"
    "2. quote — точная подстрока поста, скопированная символ в символ.\n"
    "3. phase — так, как окно названо в тексте: gtd, wl, public, ratlist, team, "
    "holder claim. Не подгоняй под чужие слова.\n"
    "4. Нечего извлечь — все поля null. Это нормальный и частый ответ.\n"
    "5. Выдуманная цитата будет отброшена проверкой, а ответ потерян целиком.\n"
    "6. Если в тексте месяц и число без года — год бери из СЕГОДНЯШНЕЙ ДАТЫ ниже, "
    "а если дата уже прошла в этом году, значит речь о следующем."
)

# Cheap on purpose: this runs over every project on a schedule, and the job is
# copying facts out of two hundred characters, not reasoning. At roughly
# $0.0001 a post the extraction is a rounding error next to the $0.005 the post
# itself costs to fetch.
# Extraction is copying two hundred characters into fields, not reasoning, so
# the cheapest competent model wins. At ~$0.0001 a post it is a rounding error
# beside the $0.005 the post costs to fetch.
#
# A list rather than a name, because both providers failed differently within
# one hour on 2026-08-17: the configured endpoint answered `500: no available
# channel` for grok while serving gemini fine, and the OpenRouter key hit a
# monthly cap set on the key itself. A scan whose provider is down produces
# nothing, which looks exactly like a scan that found nothing — the most
# expensive kind of silence. So it tries in order and takes the first answer.
OPENROUTER = "https://openrouter.ai/api/v1"
CANDIDATES = (
    ("gemini-3.5-flash", "", ""),           # configured endpoint, cheap tier
    ("gpt-5.4-mini", "", ""),               # same endpoint, different upstream
    ("google/gemini-2.5-flash-lite", OPENROUTER, "OPENROUTER_API_KEY"),
)
MIN_QUOTE = 8


def _providers() -> list[tuple[str, str, str]]:
    """(model, base_url, api_key) worth trying, in order."""
    out = []
    for model, base, env in CANDIDATES:
        if env:
            key = os.getenv(env, "").strip()
            if not key:
                continue
            out.append((model, base, key))
        else:
            out.append((model, "", ""))
    return out


def _normalize(text: str) -> str:
    """Compare on shape, not on whitespace or case.

    A model reflowing a line break or lowercasing an emoji-heavy headline is
    not a fabrication, and refusing it would throw away true findings. Anything
    beyond that has to match.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_is_real(quote: str, post_text: str) -> bool:
    """Is this quote actually in the post? The whole gate, in one function."""
    q = _normalize(quote)
    if len(q) < MIN_QUOTE:
        return False
    return q in _normalize(post_text)


class ExtractionDown(Exception):
    """Every provider refused. Not the same as "the post said nothing".

    This distinction cost real trust once: a scan hit the OpenRouter key's
    monthly cap on its second project, every call raised, the exceptions were
    swallowed, and the run reported "0 findings" for a project whose newest
    post was a mint manifesto with a date, a supply and a price. That silence
    was then read — by me — as evidence the project had announced nothing.
    """


def _ask(post_text: str) -> dict[str, Any] | None:
    # The model does not know what day it is — measured, not assumed: asked
    # directly, both endpoints answered with dates from 2024 and 2025 while it
    # was August 2026. A blurb saying "18 авг" then became 2024-08-18, which is
    # a mint tomorrow filed two years in the past. So today is supplied.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system = f"{SYSTEM}\n\nСЕГОДНЯШНЯЯ ДАТА: {today} (UTC)."
    raw = ""
    failures: list[str] = []
    for model, base_url, key in _providers():
        try:
            raw = llm_client.chat_completion(
                system=system, user=post_text[:2000],
                temperature=0, max_tokens=400, timeout=90,
                model=model, base_url=base_url, api_key=key,
            )
            if raw:
                break
        except Exception as exc:  # noqa: BLE001 — try the next provider
            failures.append(f"{model}: {str(exc)[:80]}")
            continue
    if not raw:
        # Nobody answered. Raising rather than returning None so the caller
        # cannot mistake a broken provider for a quiet project.
        raise ExtractionDown("; ".join(failures) or "пустой ответ")
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean_supply(value: Any) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    if not digits:
        return None
    n = int(digits)
    return n if 0 < n < 100_000_000 else None


def extract_from_post(post: dict[str, Any]) -> dict[str, Any] | None:
    """One post in, one verified finding out — or nothing.

    Returns None both when the post says nothing about a mint and when the
    model claimed something it could not quote. Those two are deliberately the
    same outcome: an unquotable claim is not a weaker finding, it is not a
    finding.
    """
    text = post.get("text") or ""
    if not text.strip():
        return None
    data = _ask(text)          # raises ExtractionDown when no provider answers
    if not data:
        return None

    quote = str(data.get("quote") or "")
    supply = _clean_supply(data.get("supply"))
    price = str(data.get("price") or "").strip()[:60]
    when = bot_mints.parse_when(str(data.get("mint_date") or "").replace("UTC", "").strip())
    raw_phase = str(data.get("phase") or "tba").strip()
    phase = bot_mints.normalize_phase(raw_phase)
    phase_label = "" if phase in bot_mints.KNOWN_PHASES else raw_phase[:32]
    chain = str(data.get("chain") or "").strip()[:60]

    # A date well in the past is a year the model guessed wrong, not a mint
    # that already happened: these texts announce drops that are coming. Two
    # weeks of slack covers a stale blurb about last week's mint; beyond that
    # the date is dropped and whatever else the text said is kept.
    if when:
        try:
            moment = datetime.fromisoformat(when)
        except ValueError:
            moment = None
        if moment and moment < datetime.now(timezone.utc) - timedelta(days=14):
            when = None

    if not (supply or price or when):
        return None
    if not quote_is_real(quote, text):
        # Silent on purpose. This is the expected failure, not an incident, and
        # a log line per skipped post would bury the ones that matter.
        return None

    return {
        "supply": supply,
        "chain": chain,
        "phases": [{"kind": phase, "label": phase_label,
                    "starts_at": when, "price": price}],
        "note": "",
        "source": "x",
        "quote": quote[:600],
        "post_url": post.get("url") or "",
        "post_id": post.get("id") or "",
        "created_at": post.get("created_at") or "",
    }


def scan_project(project) -> list[int]:
    """Read this project's new posts and queue whatever survives the check.

    Returns the ids of proposals created. Anything already proposed for the
    same post is skipped, so a re-scan after a crash does not ask twice.
    """
    handle = x_client.handle_from_url(getattr(project, "twitter_url", ""))
    if not handle:
        return []
    posts = x_client.recent_posts(handle, project_id=project.id, limit=10)
    made: list[int] = []
    seen_key = f"x_seen_posts.{project.id}"
    # dict.fromkeys keeps insertion order: trimming the tail must evict the
    # OLDEST ids, not hash-order-random ones (which could drop today's post
    # and re-propose it after a restart).
    seen = dict.fromkeys(
        p for p in (bot_db.get_setting(seen_key, "") or "").split(",") if p
    )
    for post in posts:
        if post["id"] in seen:
            continue
        try:
            finding = extract_from_post(post)
        except ExtractionDown:
            # Do not mark it seen: the post has not been read, and pretending
            # otherwise would lose it forever behind `since_id`.
            raise
        seen[post["id"]] = None
        if not finding:
            continue
        made.append(bot_mints.add_proposal(
            project.id, finding, quote=finding["quote"], source_url=finding["post_url"]))
    # Keep the tail bounded; `since_id` is the real guard and this only stops a
    # re-read of the same page proposing twice.
    bot_db.set_setting(seen_key, ",".join(list(seen)[-60:]))
    return made


def seed_from_blurbs(projects, mint_info_of) -> list[int]:
    """Read the hand-written `mint_info` lines the same way a tweet is read.

    Seventeen projects carry a sentence somebody typed months ago —
    "18 авг · 13:00 UTC · 444 шт · 0.0016 ETH" — and the calendar was throwing
    all of it away because it is prose. It is prose *we hold*, which is the
    only property the quote check needs, so it goes through exactly the same
    gate: the model must quote the blurb verbatim or the finding is dropped.

    Still proposals, not entries. The blurb is a human's summary of an
    announcement, not the announcement, and it can be stale — which is a good
    reason to look at it before publishing, not a reason to trust it more than
    a tweet.
    """
    made: list[int] = []
    for project in projects:
        blurb = (mint_info_of(project) or "").strip()
        if len(blurb) < 12:
            continue
        try:
            finding = extract_from_post({
                "id": f"toml:{project.id}", "text": blurb,
                "url": "", "created_at": "",
            })
        except ExtractionDown:
            continue
        if not finding:
            continue
        finding["source"] = "toml"
        made.append(bot_mints.add_proposal(
            project.id, finding, quote=finding["quote"], source_url=""))
    return made


def scan_all(projects) -> dict[str, Any]:
    """Walk every project that has a handle. One bad project never stops the rest."""
    made, errors, looked, unread = [], [], 0, []
    for project in projects:
        if not x_client.handle_from_url(getattr(project, "twitter_url", "")):
            continue
        looked += 1
        try:
            made.extend(scan_project(project))
        except ExtractionDown as exc:
            # Reported apart from other errors and counted, because "0 findings
            # with 6 projects unread" and "0 findings" mean opposite things.
            unread.append(project.id)
            errors.append(f"{project.id}: разбор недоступен ({exc})")
        except x_client.XError as exc:
            errors.append(f"{project.id}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{project.id}: {type(exc).__name__}: {exc}")
    return {"projects": looked, "proposals": made, "errors": errors,
            "unread": unread}
