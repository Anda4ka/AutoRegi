"""How a proposal looks when it reaches the operator, and how a person edits one.

The message has one job: make it cheap to disbelieve. Every proposal arrives
with the sentence it was read from and a link to the post that sentence came
from, so accepting is a decision made against evidence rather than against a
model's confidence. The quote has already been checked in code — it is
verbatim from the fetched post — so what the operator is judging is whether the
post means what the extractor thought, not whether the post exists.

What is already known is shown next to what is proposed. Most of these will be
small corrections to a project already in the calendar, and a diff is far
faster to judge than a fresh card.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from bot import mints as bot_mints

PHASE_ORDER = {k: i for i, k in enumerate(bot_mints.PHASES)}


def esc(text: str) -> str:
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def when_text(iso: str | None) -> str:
    if not iso:
        return "TBA"
    try:
        moment = datetime.fromisoformat(iso).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return "TBA"
    return moment.strftime("%d.%m.%Y %H:%M UTC")


def phase_label(kind: str, has_date: bool, label: str = "") -> str:
    """What to call a phase on screen.

    `tba` is not a phase, it is the absence of one — the extractor uses it when
    a text says "mint" without saying which window. Printing it as a label next
    to a date that also says TBA produced "TBA TBA", and next to a real date it
    claimed a phase the source never mentioned. So: with a date it is simply
    the mint, without one it says nothing and the date line carries the news.
    """
    if label:
        return label
    if kind == "tba":
        return "Минт" if has_date else ""
    return bot_mints.phase_label(kind)


def proposal_text(proposal: dict[str, Any], project_name: str,
                  current: dict[str, Any] | None) -> str:
    payload = proposal.get("payload") or {}
    lines = [f"🔎 <b>{esc(project_name)}</b> — нашёл в X", ""]

    if payload.get("supply"):
        was = (current or {}).get("supply")
        mark = f" (было {was})" if was and was != payload["supply"] else ""
        lines.append(f"саплай: <b>{payload['supply']}</b>{mark}")
    if payload.get("chain"):
        lines.append(f"сеть: <b>{esc(payload['chain'])}</b>")
    for phase in (payload.get("phases") or []):
        label = phase_label(str(phase.get("kind") or ""), bool(phase.get("starts_at")),
                            str(phase.get("label") or ""))
        price = f" · {esc(phase.get('price'))}" if phase.get("price") else ""
        head = f"{label}: " if label else ""
        lines.append(f"{head}<b>{when_text(phase.get('starts_at'))}</b>{price}")

    quote = (proposal.get("quote") or "").strip()
    if quote:
        # The whole reason this message is trustworthy. Shown in full rather
        # than trimmed to a preview: a quote cut short is a quote you cannot
        # check against the post.
        lines += ["", "<i>Цитата из поста:</i>", f"<blockquote>{esc(quote)}</blockquote>"]
    if proposal.get("source_url"):
        lines.append(f'<a href="{esc(proposal["source_url"])}">открыть пост</a>')

    if current and current.get("phases"):
        known = ", ".join(
            f"{bot_mints.PHASE_RU.get(p['kind'], p['kind'])} {when_text(p['starts_at'])}"
            for p in current["phases"][:3]
        )
        lines += ["", f"<i>сейчас в календаре:</i> {esc(known)}"]
    elif not current:
        lines += ["", "<i>в календаре этого проекта ещё нет</i>"]
    return "\n".join(lines)


def digest_text(proposals: list[dict], name_of) -> str:
    """All pending findings in one message, each shown as what it would write.

    Twelve separate cards is friction the operator did not ask for, and
    friction here has a cost: unconfirmed findings mean the calendar has no
    dates, which means it cannot sort by the next mint — the whole point of
    the screen. So the digest shows the parsed result of each, which is what
    accepting would actually do, and the per-item cards stay available for
    anything that needs its quote read.
    """
    lines = [f"🔎 <b>Ждут подтверждения: {len(proposals)}</b>", ""]
    for row in proposals:
        payload = row.get("payload") or {}
        bits = []
        if payload.get("supply"):
            bits.append(f"{payload['supply']} шт")
        for phase in (payload.get("phases") or []):
            has_date = bool(phase.get("starts_at"))
            label = phase_label(str(phase.get("kind") or ""), has_date,
                                str(phase.get("label") or ""))
            when = when_text(phase.get("starts_at")) if has_date else "дата не объявлена"
            price = f" {esc(phase.get('price'))}" if phase.get("price") else ""
            bits.append(" ".join(x for x in (label, when + price) if x).strip())
        if payload.get("chain"):
            bits.append(esc(payload["chain"]))
        source = "из X" if row.get("source_url") else "из описания"
        lines.append(f"• <b>{esc(name_of(row['project_id']))}</b> — "
                     f"{esc(' · '.join(bits)) or 'без полей'} <i>({source})</i>")
    lines += ["", "«Принять все» запишет всё перечисленное в календарь.",
              "«По одной» пришлёт каждую с цитатой и ссылкой на источник."]
    return "\n".join(lines)


# ------------------------------------------------------------------ /mint


_FIELD = re.compile(r"^(\w+)\s+(.*)$", re.S)


def clear_target(raw: str) -> str | None:
    """The project id a `/mint <id> [ | ] clear` body wants removed, or None.

    Both the bare form (``foo clear``) and the pipe form (``foo | clear``) —
    the general separator this command uses everywhere else — mean "take it
    off the calendar". Anything after "clear" is not a clear: the caller will
    parse it and refuse it for a reason worth reading.
    """
    parts = (raw or "").split(maxsplit=1)
    if len(parts) != 2:
        return None
    if parts[1].lstrip("|").strip().lower() != "clear":
        return None
    project_id = parts[0].strip().lstrip("|").strip().lower()
    return project_id or None


def parse_mint_command(raw: str) -> tuple[str, dict[str, Any], list[str]]:
    """Parse `/mint <project> | supply 2222 | gtd 22.08.2026 15:00 free`.

    Returns (project_id, payload, errors). Segments are split on `|` because
    dates contain spaces and prices contain both spaces and dots; any
    space-delimited scheme would need quoting rules that nobody remembers.

    An unreadable date is an error, never a silent TBA. The two look identical
    on the calendar and mean opposite things.
    """
    parts = [p.strip() for p in (raw or "").split("|")]
    if not any(parts):
        return "", {}, ["пусто"]

    # The first segment is the project, even when it is empty. Dropping empties
    # first made `| supply 5` read "supply" as the project name — a wrong
    # answer where an error was the honest one.
    head = parts[0].split()
    project_id = head[0].strip().lower() if head else ""
    parts = [p for p in parts[1:] if p]
    payload: dict[str, Any] = {"phases": []}
    errors: list[str] = []

    for segment in parts:
        match = _FIELD.match(segment.strip())
        if not match:
            errors.append(f"не разобрал «{segment[:40]}»")
            continue
        raw_key, value = match.group(1), match.group(2).strip()
        key = raw_key.lower()
        if key == "supply":
            digits = re.sub(r"[^\d]", "", value)
            if not digits:
                errors.append(f"саплай «{value[:20]}» — не число")
            else:
                payload["supply"] = int(digits)
        elif key == "chain":
            payload["chain"] = value[:60]
        elif key in ("mint", "mint_url"):
            # The page people actually mint on. Kept apart from the project's
            # site because for half these projects the mint lives on OpenSea or
            # a launchpad, and a "Минт" button that opens a landing page wastes
            # the one tap that mattered.
            payload["mint_url"] = value[:400]
        elif key == "note":
            payload["note"] = value[:400]
        elif key == "name":
            payload["name"] = value[:60]
        else:
            # `<phase> <date and time> [price]`: the date is the leading part
            # that parses, the rest is price. Tried longest-first so that
            # "22.08.2026 15:00 free" does not read the time as a price.
            words = value.split()
            when, price, taken = None, "", 0
            for take in range(min(3, len(words)), 0, -1):
                candidate = " ".join(words[:take])
                parsed = bot_mints.parse_when(candidate)
                if parsed:
                    when, taken = parsed, take
                    break
            if when is None and words and words[0].lower() not in ("tba", "-", "—"):
                errors.append(f"{key}: не понял дату «{value[:30]}»")
                continue
            if when is None:
                taken = 1
            price = " ".join(words[taken:]).strip()[:60]
            payload["phases"].append({
                "kind": bot_mints.normalize_phase(key),
                "label": "" if key in bot_mints.KNOWN_PHASES else raw_key,
                "starts_at": when, "price": price,
            })

    if not project_id:
        errors.append("не указан проект")
    return project_id, payload, errors


USAGE = (
    "<b>/mint</b> — правка календаря\n\n"
    "<code>/mint demob | supply 2222 | chain Robinhood | "
    "gtd 22.08.2026 15:00 free | public 22.08.2026 18:00 0.02 ETH</code>\n\n"
    "Разделитель — <code>|</code>, потому что в датах и ценах есть пробелы.\n"
    "Поля: <code>supply</code>, <code>chain</code>, <code>note</code>, "
    "<code>mint</code> (ссылка), <code>name</code> (для проектов вне бота).\n"
    "Всё остальное — <b>название фазы</b>, любое: "
    "<code>gtd</code>, <code>wl</code>, <code>public</code>, "
    "<code>ratlist</code>, <code>team</code>, <code>holder</code>.\n"
    "Фаза: <code>&lt;фаза&gt; &lt;дата и время UTC&gt; [цена]</code>, "
    "или <code>&lt;фаза&gt; tba [цена]</code>.\n"
    "Дата: <code>22.08.2026 15:00</code> или <code>2026-08-22 15:00</code>.\n\n"
    "<code>/mint democ | mint https://opensea.io/collection/democ</code>\n\n"
    "<code>/mint &lt;проект&gt; clear</code> — убрать проект из календаря."
)
