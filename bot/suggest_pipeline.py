"""Suggest→draft→probe pipeline: a suggestion becomes a tested adapter on its own.

Pilot scope (operator decision, 2026-09-08): everything up to the live probe
runs unattended — recon, refusal classes, the LLM verdict behind the quote
gate, a draft folder from the nearest template, and a 10-row smoke on
**generated** wallets. What deliberately stays human: `enabled = true` and
the announcement. The dossier an admin receives is the whole case file; the
one thing it never does is hide a step.

    SUGGEST_PIPELINE=1     run the pilot sweep (off by default)
    SUGGEST_PROBE_ROWS=10  rows per live probe (operator rule: 10, not 100)
    SUGGEST_AUTO_ADD=0     future: flip enabled=true when the probe passed.
                           0 keeps the human gate; do not raise it until the
                           pilot has a week without an incident.

Hard lines the code holds on its own:

* **Probe wallets are generated** (`eth_account.Account.create()`), never a
  user's or the operator's `/data`. An unvetted suggestion is exactly the
  wrong party to hand real wallets to — `onchainwine` was a scam, and a
  form that accepts a wallet is not evidence the site deserves one.
* **Captcha ⇒ refuse.** A solve costs operator money and captcha projects
  are VIP-only by operator rule; the pipeline has no business deciding to
  spend either. Same for wallet-signature flows (needs a private key).
* **The LLM quotes or it did not happen.** Every claim in the verdict must
  be a substring of a fetched source (whitespace-normalised — bundles are
  minified); an unverifiable claim fails the gate, the same contract as
  the mint scanner (`bot/mint_scan.py`). No LLM key configured ⇒ the
  verdict step is skipped and the dossier says so — the probe is still the
  authority on what the route actually answers.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlparse

from bot import config
from bot import db as bot_db
from bot.http_client import load_proxies
from bot.registry import list_projects, load_project_folder

ROOT = Path(__file__).resolve().parents[1]
OPERATOR_PROBE_USER = int(os.getenv("SUGGEST_PROBE_USER", "0") or 0)  # probe rows land here for dedup
ID_RE = re.compile(r"^[a-z0-9_]{2,20}$")

_busy = threading.Lock()
_scaffold_cache = None


def load_scaffold():
    """tools/scaffold_project.py is not a package member; load it once.

    The cache lives in `_scaffold_cache`, not in this function's name: a
    loader that overwrote itself with its result worked once and then
    answered 'function' object has no attribute 'gather' on every later
    suggestion (caught live on suggestion #56, 2026-09-08).
    """
    global _scaffold_cache
    if _scaffold_cache is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "scaffold_project", ROOT / "tools" / "scaffold_project.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _scaffold_cache = module
    return _scaffold_cache


def pipeline_enabled() -> bool:
    return os.environ.get("SUGGEST_PIPELINE", "0").strip() in ("1", "true", "yes")


def probe_rows() -> int:
    try:
        return max(1, min(10, int(os.environ.get("SUGGEST_PROBE_ROWS", "10"))))
    except ValueError:
        return 10


# TLDs accepted for a *bare* domain (no scheme) in a suggestion. Full
# http(s) URLs never pass through this list. Kept to the ones real project
# sites actually use; an exotic TLD without a scheme just needs the scheme.
_BARE_TLDS = frozenset(
    """
    app art auction bet biz club com coop education exchange fit fun game
    games global help homes income io jobs life link live lol love market
    money museum name network nft one online open party place pro quest
    rsvp school shop site space store tech token tools trade trade tube
    unicom vip web website wiki world xyz xyz zero zone
    ai co dev es eu fm gg gs in me nc net nu nz org ru sh so to tv ua uk us
    va vc
    """.split()
)


def extract_url(body: str) -> str:
    """First URL in a suggestion body, '' if none.

    Prefers a full ``http(s)://…`` URL; a **bare domain** is accepted and
    read as https. Suggestion #61 was refused as «нет URL» because the user
    typed ``brokersdesks.xyz`` with no scheme — to a human that is a link,
    so the cheapest gate should not bounce it before anything is checked.

    The bare-domain shape is deliberately narrow: ASCII labels only (so a
    Cyrillic sentence with a full stop cannot match), at least two labels,
    a TLD from a common allowlist (so a handle like ``vasya.petrov`` does
    not become a link), and no ``@`` before it (e-mails are not links).
    Trailing sentence punctuation is stripped.
    """
    text = body or ""
    m = re.search(r"https?://[^\s<>()\"']+", text, re.I)
    if m:
        return m.group(0).rstrip(".,…")
    m = re.search(
        r"(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+([a-z]{2,24}))"
        r"(/[^\s<>()\"']*)?",
        text,
        re.I,
    )
    if not m or m.group(2).lower() not in _BARE_TLDS:
        return ""
    domain = m.group(1)
    # A bare host like `localhost` or an IP has no dot-separated alphabetic
    # TLD and cannot have matched; but guard the single-label case anyway.
    if "." not in domain.strip("."):
        return ""
    return "https://" + m.group(0).rstrip(".,…")


def project_id_for(url: str, taken: set[str] | None = None) -> str:
    """Domain label → project id; '' if it collides or does not sanitise."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    label = host.split(".")[0]
    label = re.sub(r"[^a-z0-9_]", "", label)
    if not ID_RE.match(label):
        return ""
    taken = taken if taken is not None else set()
    if label in taken or (ROOT / "projects" / label).exists():
        return ""
    return label


# --- refusal classes the code decides without any model ----------------------

GOOGLE_FORM_RE = re.compile(r"docs\.google\.com/forms/", re.I)


def _fb_public_load_data(sources: dict[str, str]) -> list | None:
    """Parsed FB_PUBLIC_LOAD_DATA_ from a fetched viewform page, or None."""
    import json as _json

    for text in sources.values():
        m = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);", text, re.S)
        if not m:
            continue
        try:
            return _json.loads(m.group(1))
        except ValueError:
            continue
    return None


def google_form_requires_login(url: str, sources: dict[str, str]) -> str | None:
    """A viewform page that serves anonymously but POSTs 401.

    The two markers this repo learned the hard way (AGENTS.md §3.1):
    `FB_PUBLIC_LOAD_DATA_[1][10][1] == 1` (verified email collection) and
    top-level `[18] == 1` — working forms carry None/0. A marker alone is a
    suspicion, so when one fires the form gets one live junk POST: `401`
    confirms the refusal without creating a row; an unexpected 2xx means the
    markers lied and the form is left alone.
    """
    data = _fb_public_load_data(sources)
    if data is None:
        if any(GOOGLE_FORM_RE.search(text) for text in sources.values()):
            return "google form: FB_PUBLIC_LOAD_DATA_ не читается — автомат не берётся"
        return None
    try:
        email_collect = data[1][10][1]
    except (IndexError, TypeError):
        email_collect = None
    needs_login_marker = bool(len(data) > 18 and data[18] == 1)
    if not needs_login_marker and email_collect != 1:
        return None

    import requests

    try:
        r = requests.post(
            url.replace("/viewform", "/formResponse"),
            data={"entry.000000001": "probe", "fvv": "1"},
            headers={"Origin": "https://docs.google.com", "Referer": url},
            timeout=(10, 25), allow_redirects=False,
        )
    except requests.RequestException:
        return "google form с маркерами входа в Google ([18]==1 / email) — POST не удался, руками"
    if r.status_code == 401:
        return ("google form требует вход в Google-аккаунт: formResponse отвечает 401 "
                f"(маркеры: email={email_collect}, [18]={data[18] if len(data) > 18 else '?'}) "
                "— класс отказа, адаптер не пишется")
    return None  # markers lied — the probe answered like a working form


# --- LLM verdict behind the quote gate ----------------------------------------

VERDICTS = ("form", "not_form", "needs_captcha", "needs_signature", "closed", "unclear")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def check_quotes(quotes: list[str], sources: dict[str, str]) -> list[str]:
    """Quotes that are really in the sources (whitespace-normalised)."""
    squashed = [_squash(t) for t in sources.values()]
    ok: list[str] = []
    for q in quotes:
        qn = _squash(q)
        if len(qn) >= 12 and any(qn in s for s in squashed):
            ok.append(q)
    return ok


def llm_verdict(sources: dict[str, str], endpoints: dict) -> dict:
    """Ask the model what the page is; every claim must be quotable.

    Returns {"verdict": str, "reason": str, "quotes_ok": n, "quotes_total": n,
    "configured": bool}. Unquotable or unparseable ⇒ "unclear", which the
    caller treats as a refusal: a guess that reaches a dossier looks like
    evidence (the mint scanner was built on exactly that incident).
    """
    from bot import llm_client

    if not llm_client.has_llm():
        return {"verdict": "skipped", "reason": "LLM не настроен",
                "quotes_ok": 0, "quotes_total": 0, "configured": False}

    found_lines = "\n".join(
        f"{kind}: " + json.dumps([e["url"] for e in items[:3]])
        for kind, items in endpoints.items()
    ) or "(эндпоинты не найдены)"
    page = (max(sources.values(), key=len)[:12000]) if sources else ""
    system = (
        "Ты классифицируешь страницу whitelist-формы NFT-проекта. Ответ — только "
        "JSON: {\"verdict\": one of " + json.dumps(list(VERDICTS)) + ", "
        "\"reason\": \"одно предложение по-русски\", "
        "\"quotes\": [\"дословные фрагменты из исходника, подтверждающие вердикт\"]}. "
        "Каждая цитата должна быть дословной строкой из присланного исходника. "
        "verdict=needs_captcha если форма требует капчу (turnstile/recaptcha/"
        "hcaptcha) в самом потоке подачи; needs_signature если подача требует "
        "подпись кошелька (SIWE/personal_sign) — просто наличие connect-кнопки "
        "не повод; closed если форма явно закрыта; form если это рабочий "
        "whitelist/allowlist/raffle-запрос; not_form если это не форма."
    )
    try:
        raw = llm_client.chat_completion(
            system=system,
            user=f"НАЙДЕННЫЕ ЭНДПОИНТЫ:\n{found_lines}\n\nИСХОДНИК СТРАНИЦЫ:\n{page}",
            temperature=0.2,
            max_tokens=400,
        )
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as exc:  # noqa: BLE001 — any provider failure is "unclear"
        return {"verdict": "unclear", "reason": f"LLM error: {type(exc).__name__}",
                "quotes_ok": 0, "quotes_total": 0, "configured": True}

    quotes = [str(q) for q in data.get("quotes") or [] if str(q).strip()]
    verified = check_quotes(quotes, sources)
    verdict = str(data.get("verdict") or "unclear")
    if verdict not in VERDICTS:
        verdict = "unclear"
    if quotes and not verified:
        # Every claim failed the substring check: fabrication, not evidence.
        verdict = "unclear"
    return {
        "verdict": verdict,
        "reason": str(data.get("reason") or "")[:300],
        "quotes_ok": len(verified),
        "quotes_total": len(quotes),
        "configured": True,
    }


REFUSAL_VERDICTS = {
    "needs_captcha": "капча — строки тратят баланс солвера (правило: только VIP или свой ключ)",
    "needs_signature": "подача требует подпись кошелька — нужен приватный ключ, это standalone",
    "not_form": "модель считает, что это не форма регистрации",
    "closed": "форма закрыта по словам самой страницы",
    "unclear": "вердикт не прошёл цитатный гейт — подтверждения нет",
}


# --- generated-wallet probe ----------------------------------------------------

def _probe_accounts(n: int) -> list[tuple[str, str]]:
    from eth_account import Account

    out = []
    for _ in range(max(1, n)):
        acct = Account.create()
        handle = f"wl{secrets.token_hex(4)}"  # <=15 chars, [a-z0-9_]
        out.append((handle, acct.address.lower()))
    return out


def probe_project(project, proxies: list[str], n: int) -> list[dict]:
    """File n generated rows through the draft adapter; record settled ones."""
    results = []
    for i, (handle, wallet) in enumerate(_probe_accounts(n)):
        fields = project.parse(f"{handle}|{wallet}") or {"handle": handle, "wallet": wallet}
        error = project.validate(fields)
        if error:
            results.append({"status": "BAD_RESP", "detail": f"validate: {error}",
                            "wallet": wallet, "handle": handle})
            continue
        try:
            res = project.submit(
                fields, proxies=proxies, proxy_idx=i,
                retries=3, retry_delay=3.0, min_delay=0.2, max_delay=0.8,
            )
        except Exception as exc:  # noqa: BLE001 — a draft adapter may raise
            res = {"status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}
        res = dict(res)
        res.setdefault("wallet", wallet)
        res.setdefault("handle", handle)
        results.append(res)
        if res.get("status") in ("OK", "ALREADY"):
            try:
                bot_db.record_submission(
                    OPERATOR_PROBE_USER, project.id,
                    res.get("handle") or handle, res.get("wallet") or wallet,
                    res["status"], str(res.get("detail") or "")[:500],
                )
            except Exception:  # noqa: BLE001 — bookkeeping must not kill a probe
                pass
    return results


# --- the state machine ---------------------------------------------------------

def process_suggestion(sug: dict, send) -> None:
    """One suggestion → one pipeline run with a dossier at the end."""
    sid = sug["id"]
    url = extract_url(sug.get("body") or "")
    state = {"url": url or "(нет URL)", "project_id": "", "steps": []}

    def step(status: str, report: str, evidence: dict | None = None) -> None:
        state["steps"].append(status)
        bot_db.upsert_pipeline_run(
            sid, url or "-", project_id=state["project_id"], status=status,
            report=report, evidence=json.dumps(evidence or {}, ensure_ascii=False),
        )

    if not url:
        report = _dossier(sug, state, "ОТКАЗ: в предложке нет URL")
        step("refused", report)
        _send_dossier(send, report)
        return

    try:
        report, status = _run(sid, sug, url, state, step)
    except Exception as exc:  # noqa: BLE001 — the sweep must survive anything
        report = _dossier(sug, state, f"ОШИБКА ПАЙПЛАЙНА: {type(exc).__name__}: {exc}")
        step("error", report)
    _send_dossier(send, report)


def _run(sid: int, sug: dict, url: str, state: dict, step) -> tuple[str, str]:
    scaffold = load_scaffold()
    state["url"] = url

    # A post on X is where projects announce, but the scout cannot read X
    # (login wall) and there is no form to find there. Refusing beats an
    # error dossier: the suggestion needs a human to read the post and find
    # the site — or the suggester to send the site itself.
    if re.search(r"^https?://(www\.)?(x|twitter)\.com/", url, re.I):
        report = _dossier(sug, state,
                          "ОТКАЗ: ссылка на пост X — разведчик X не читает. "
                          "Нужен сайт формы; найти его по посту может человек.")
        step("refused", report)
        return report, "refused"

    # An already-served project must not cost a recon: the host check is one
    # dict lookup, and suggestion #58/#59 each burned a full scout sweep
    # (dozens of fetches) just to be told they were duplicates.
    host = urlparse(url).netloc.lower().removeprefix("www.")
    known_hosts = {
        urlparse(p.link).netloc.lower().removeprefix("www.")
        for p in list_projects() if getattr(p, "link", "")
    } - {""}
    if host and host in known_hosts:
        report = _dossier(sug, state, f"ОТКАЗ: домен уже в боте — дубликат предложки")
        step("refused", report)
        return report, "refused"

    # 1. recon
    found, sources, errors = scaffold.gather(url)
    endpoints = found.get("endpoints") or {}
    state["endpoints"] = {k: [e["url"] for e in v[:2]] for k, v in endpoints.items()}
    state["read_files"] = len(sources)

    # 2. mechanical refusals
    if found.get("captcha"):
        reason = "капча на форме — строки тратят баланс солвера; добавить руками по правилам VIP/свой ключ"
        report = _dossier(sug, state, f"ОТКАЗ: {reason}")
        step("refused", report, {"captcha": found["captcha"]})
        return report, "refused"
    gform = google_form_requires_login(url, sources)
    if gform:
        report = _dossier(sug, state, f"ОТКАЗ: {gform}")
        step("refused", report)
        return report, "refused"

    kind = scaffold.best_kind(found)
    if kind == "google_form":
        # Markers said the form works (no 401 above), but no google-form
        # adapter is alive to copy — the cookbook row is the hand path.
        entries = ", ".join(found.get("form_entries") or []) or "not found"
        report = _dossier(
            sug, state,
            "ОТКАЗ: рабочая Google Form, но живого шаблона Google Form нет. "
            f"Вручную по the cookbook (PROJECTS.md): entry.* = {entries}")
        step("refused", report, {"form_entries": entries})
        return report, "refused"

    taken = {p.id for p in list_projects()}
    project_id = project_id_for(url, taken)
    if not project_id:
        reason = "не придумать безопасный id (домен занят/не санитизируется) — руками"
        report = _dossier(sug, state, f"ОТКАЗ: {reason}")
        step("refused", report)
        return report, "refused"
    state["project_id"] = project_id

    # 3. LLM verdict behind the quote gate (optional without a key)
    verdict = llm_verdict(sources, endpoints)
    state["llm"] = verdict
    if verdict["verdict"] in REFUSAL_VERDICTS:
        reason = REFUSAL_VERDICTS[verdict["verdict"]]
        report = _dossier(sug, state,
                          f"ОТКАЗ: {reason}\nМодель: {verdict['reason']} "
                          f"(цитат проверено {verdict['quotes_ok']}/{verdict['quotes_total']})")
        step("refused", report, {"llm": verdict})
        return report, "refused"

    # 4. draft from the nearest live template
    path, todo = scaffold.init_project(project_id, url, found)
    state["draft"] = str(path)
    kind = scaffold.best_kind(found)
    state["kind"] = kind
    _endpoint_rewrite(project_id, kind, endpoints)
    step("drafted", "", {"kind": kind})

    # 5. import gate: the draft must load and parse like any project
    project = load_project_folder(project_id)
    if project is None:
        report = _dossier(sug, state, "ОТКАЗ: черновик не импортируется — дописать руками")
        step("import_failed", report)
        return report, "import_failed"
    try:
        probe_handle, probe_wallet = _probe_accounts(1)[0]
        fields = project.parse(f"{probe_handle}|{probe_wallet}")
        assert fields and project.validate(fields) is None
    except Exception:  # noqa: BLE001 — any parse/validate break fails the gate
        report = _dossier(sug, state, "ОТКАЗ: parse/validate черновика не проходит — дописать руками")
        step("import_failed", report)
        return report, "import_failed"

    # 6. live probe on generated wallets
    proxies = load_proxies(config.PROXY_FILE)
    if not proxies:
        report = _dossier(sug, state, "ОТКАЗ: нет общего пула прокси")
        step("probe_failed", report)
        return report, "probe_failed"
    rows = probe_project(project, proxies, probe_rows())
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    state["probe"] = counts
    details = sorted({str(r.get("detail") or "")[:120] for r in rows
                      if r.get("status") not in ("OK", "ALREADY")})[:3]
    state["probe_details"] = details
    settled = counts.get("OK", 0) + counts.get("ALREADY", 0)
    if settled < max(1, probe_rows() - 2):
        report = _dossier(sug, state,
                          f"ПРОБА НЕ ПРОШЛА: {settled}/{len(rows)} осело "
                          f"({', '.join(f'{k}×{v}' for k, v in counts.items())}) — "
                          "адаптер дописать руками, эндпоинт найден верно")
        step("probe_failed", report, {"probe": counts})
        return report, "probe_failed"

    # 7. passed: pilot stops at the dossier, the human flips enabled=true.
    verdict_note = (
        f"Модель: {verdict['reason']} "
        f"(цитат {verdict['quotes_ok']}/{verdict['quotes_total']})"
        if verdict["configured"] else "LLM не настроен — фильтра вердиктом не было"
    )
    enable_line = (
        f"ГОТОВО: {settled}/{len(rows)} строк легло. Включение — руками:\n"
        f"  projects/{project_id}/project.toml → enabled = true, потом /reload\n"
        f"  перед включением заполнить twitter_url и mint_info (анонс рендерится из них)"
        if os.environ.get("SUGGEST_AUTO_ADD", "0").strip() != "1"
        else "SUGGEST_AUTO_ADD=1: включить projects/" + project_id
    )
    report = _dossier(sug, state,
                      f"ПРОБА ПРОШЛА: {settled}/{len(rows)} строк легло. {verdict_note}\n{enable_line}")
    step("tested", report, {"probe": counts, "llm": verdict})
    return report, "tested"


def _endpoint_rewrite(project_id: str, kind: str, endpoints: dict) -> None:
    """Substitute the discovered endpoint into the draft's REGISTER_URL, if any.

    Only the mechanical part: an absolute URL into a string constant. What the
    endpoint *means* (success wording, ALREADY shape) is what the probe is for.
    """
    urls = [e["url"] for items in endpoints.values() for e in items
            if e["url"].startswith("http") and "firestore.googleapis.com" not in e["url"]]
    if not urls:
        return
    best = sorted(urls, key=lambda u: 0 if re.search(
        r"/(api|submit|register|apply|join|whitelist|wl)(/|$)", u) else 1)[0]
    api_path = ROOT / "projects" / project_id / "api.py"
    if not api_path.exists():
        return
    text = api_path.read_text(encoding="utf-8")
    if "REGISTER_URL" in text:
        text = re.sub(r'^REGISTER_URL = .*$', f'REGISTER_URL = "{best}"',
                      text, count=1, flags=re.M)
        api_path.write_text(text, encoding="utf-8")


# --- dossier -------------------------------------------------------------------

def _dossier(sug: dict, state: dict, verdict_line: str) -> str:
    who = f"@{sug.get('username')}" if sug.get("username") else (sug.get("first_name") or "—")
    lines = [
        f"🤖 Автопайплайн предложки #{sug.get('id')} от {who} (id {sug.get('user_id')})",
        f"URL: {state.get('url')}",
        f"Файлов прочитано: {state.get('read_files', 0)}"
        + (f" · тип: {state.get('kind')}" if state.get("kind") else ""),
    ]
    eps = state.get("endpoints") or {}
    if eps:
        lines.append("Эндпоинты: " + "; ".join(
            f"{k} → {', '.join(v)}" for k, v in list(eps.items())[:2]))
    llm = state.get("llm")
    if llm:
        lines.append(f"Вердикт LLM: {llm.get('verdict')} ({llm.get('reason')}, "
                     f"цитат {llm.get('quotes_ok')}/{llm.get('quotes_total')})")
    probe = state.get("probe")
    if probe:
        lines.append("Проба (сгенерированные кошельки): "
                     + ", ".join(f"{k}×{v}" for k, v in probe.items()))
    for d in state.get("probe_details") or []:
        lines.append(f"  отказ: {d}")
    if state.get("draft"):
        lines.append(f"Черновик: {state['draft']}")
    lines.append("")
    lines.append(verdict_line)
    return "\n".join(lines)


def _send_dossier(send, report: str) -> None:
    for aid in config.ADMIN_IDS:
        try:
            send(aid, report)
        except Exception:  # noqa: BLE001 — one admin's dead chat is not a stop
            pass


def maybe_sweep(send) -> None:
    """One suggestion per sweep, off the alerts thread, if enabled and idle."""
    if not pipeline_enabled():
        return
    if not _busy.acquire(blocking=False):
        return

    def _worker() -> None:
        try:
            rows = bot_db.unprocessed_suggestions(limit=1)
            if rows:
                process_suggestion(rows[0], send)
        except Exception as exc:  # noqa: BLE001 — never take the alerts loop down
            print(f"[suggest-pipeline] sweep error: {exc}", flush=True)
        finally:
            _busy.release()

    threading.Thread(target=_worker, name="suggest-pipeline", daemon=True).start()
