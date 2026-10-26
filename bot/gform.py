"""Generic Google Form adapter — the /gform builder plus the runtime.

Variant A of the «вставь ссылку на форму» idea (operator-only, 2026-09-10):
an admin pastes a public Google Form URL, the bot parses
``FB_PUBLIC_LOAD_DATA_`` (the same trick the suggest pipeline already uses),
checks the refusal classes the repo learned the hard way — a form that needs
a signed-in Google account answers 401 on ``formResponse``, and a page that
renders a captcha must never be filed with an empty token (§16.0) — shows the
fields for mapping, and writes a **disabled** ``projects/gf…/`` folder. The
operator smoke-tests it on his own wallets and flips ``enabled = true``,
exactly like any hand-written adapter.

The generated folder is three files: ``project.toml`` (enabled=false),
``form.json`` (url + fields + mapping — the whole adapter is data) and a
two-line ``module.py`` that delegates here. ``bot/gform.py`` is the only
code, so every generated project fixes itself when this module is fixed.

Mapping sources (one per field, ``N=…`` lines the admin replies with):

    wallet          — 0x… from the row's wallet
    handle          — twitter handle from the row
    quote_url       — synthetic x.com status URL (bot/tweet_urls.py)
    comment_url     — synthetic x.com status URL (bot/tweet_urls.py)
    option:<text>   — one of the field's own checkbox/radio values
    static:<text>   — fixed text
    (unlisted)      — skip; the entry is not sent

Rules enforced before anything is written: every **required** field must be
mapped to something other than skip, and ``wallet`` must be mapped somewhere
(the global dedup and the OK slot both key on it — a project that never
sends a wallet cannot be counted).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from bot.http_client import (
    backoff_sleep,
    choose_proxy_url,
    error_proxy_label,
    format_request_error,
    is_google_form_closed,
    is_valid_wallet,
    no_proxies_result,
    normalize_twitter,
    proxy_dict_for,
    random_fingerprint,
)
from bot.http_status import is_transient_http
from bot.tweet_urls import generate_status_url
from bot.ux import esc

ROOT = Path(__file__).resolve().parents[1]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = (15, 45)

FORM_URL_RE = re.compile(
    r"https?://docs\.google\.com/forms/d/(?:e/)?([A-Za-z0-9_-]+)/(?:viewform|edit)",
    re.I,
)
FB_RE = re.compile(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);", re.S)
# Same loader-script rule as tools/scaffold_project.py: a captcha claim is
# only real when the page actually pulls the script. A Google Form that
# renders one costs a solve per row — VIP/own-key territory, not this tool.
CAPTCHA_SCRIPT_RE = re.compile(
    r"challenges\.cloudflare\.com/turnstile|recaptcha/api\.js|hcaptcha\.com/1/api\.js",
    re.I,
)
TAG_RE = re.compile(r"<[^>]+>")
RECORDED_MARKER = "Your response has been recorded"

# Field-title guesses shown to the admin as pre-filled defaults.
TITLE_HINTS = (
    (r"evm|wallet|address|0x", "wallet"),
    (r"twitter|x[ /]|handle|username|ник", "handle"),
    (r"quote", "quote_url"),
    (r"comment|repl(y)|ответ", "comment_url"),
)


class GformError(Exception):
    """One human sentence — the /gform flow replies with it verbatim."""


# --- step 1: fetch + parse -----------------------------------------------------


def fetch_viewform(url: str) -> str:
    """GET the form page, English locale pinned (the markers below are EN)."""
    if not url.startswith("http"):
        url = "https://" + url
    sep = "&" if "?" in url else "?"
    try:
        r = requests.get(
            url + sep + "hl=en",
            headers={"User-Agent": UA, "Accept-Language": "en"},
            timeout=TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise GformError(f"форма не открылась: {type(exc).__name__}") from exc
    if r.status_code in (404, 410):
        raise GformError("форма не существует (HTTP 404/410)")
    if r.status_code != 200:
        raise GformError(f"форма ответила HTTP {r.status_code}")
    if is_google_form_closed(r):
        raise GformError("форма закрыта — не принимает ответы")
    return r.text or ""


def _title_text(raw) -> str:
    return TAG_RE.sub("", str(raw or "")).strip()


def parse_form(url: str, html: str) -> dict:
    """viewform page → {"url", "form_id", "title", "fields": [...]}."""
    m = FORM_URL_RE.search(url)
    if not m:
        raise GformError("это не ссылка docs.google.com/forms/…/viewform")
    fm = FB_RE.search(html)
    if not fm:
        raise GformError("FB_PUBLIC_LOAD_DATA_ не читается — форма не публичная?")
    try:
        data = json.loads(fm.group(1))
    except ValueError as exc:
        raise GformError("FB_PUBLIC_LOAD_DATA_ не парсится как JSON") from exc

    # Login-walled forms carry the same two markers the suggest pipeline
    # refuses on (§3.1): verified-email collection [1][10][1]==1 and [18]==1.
    try:
        if data[1][10][1] == 1:
            raise GformError("форма собирает подтверждённый email — нужен вход в Google (класс отказа)")
    except (IndexError, TypeError):
        pass
    if len(data) > 18 and data[18] == 1:
        raise GformError("форма требует вход в Google-аккаунт (класс отказа)")

    if CAPTCHA_SCRIPT_RE.search(html):
        raise GformError("на форме капча — строки тратят баланс солвера, генератор её не ставит")

    try:
        title = _title_text(data[1][8]) if len(data) > 1 and len(data[1]) > 8 else ""
        items = data[1][1] or []
    except (IndexError, TypeError):
        title, items = "", []

    # Real FB_PUBLIC_LOAD_DATA_ shape (measured on a live form, 2026-09-10):
    #   data[1][1] = [question, …]
    #   question   = [poster_id, title, description, type, fields, …]
    #   fields     = [[entry_id, options, required?, …], …]
    #   options    = [["Вариант 1"], …] or null
    # and the form field name the POST wants is `entry.<entry_id>`. A question
    # may carry several fields (grid rows), so the loop flattens them.
    fields = []
    for q in items:
        if not isinstance(q, list) or len(q) < 5 or not isinstance(q[4], list):
            continue
        q_title = _title_text(q[1] if len(q) > 1 else "")
        for f in q[4]:
            if not isinstance(f, list) or not f or not isinstance(f[0], int):
                continue
            options = []
            if len(f) > 1 and isinstance(f[1], list):
                for opt in f[1]:
                    if isinstance(opt, list) and opt and opt[0] is not None:
                        options.append(_title_text(opt[0]))
            required = bool(len(f) > 2 and f[2] == 1)
            fields.append({
                "entry": f"entry.{f[0]}",
                "title": q_title,
                "options": [o for o in options if o],
                "required": required,
            })
    if not fields:
        raise GformError("полей в форме не найдено")
    return {"url": url, "form_id": m.group(1), "title": title, "fields": fields}


# --- step 2: mapping ------------------------------------------------------------

SOURCE_KEYS = ("wallet", "handle", "quote_url", "comment_url")


def suggest_source(field: dict) -> str:
    """Pre-filled default from the field's own title."""
    # A choice field only ever takes one of its own variants — a title hint
    # ("comment wallet here") must not talk it into a text source, which is a
    # per-row 400. Choice fields are decided before the hints run.
    if field["options"]:
        return f"option:{field['options'][0]}" if field["required"] else "skip"
    low = field["title"].lower()
    for pattern, source in TITLE_HINTS:
        if re.search(pattern, low):
            if source in ("wallet", "handle"):
                return source
            return source
    if field["required"]:
        return "static:"
    return "skip"


def parse_mapping(text: str, form: dict) -> dict[str, str]:
    """Admin reply → {entry: source}; raises GformError with all problems."""
    fields = {f["entry"]: f for f in form["fields"]}
    mapping: dict[str, str] = {}
    errors: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\d{1,2})\s*[=.]\s*(.+)$", line)
        if not m:
            errors.append(f"строка не понята: «{line[:60]}» (формат: 1=wallet)")
            continue
        idx = int(m.group(1))
        if not 1 <= idx <= len(form["fields"]):
            errors.append(f"номер {idx}: поля с таким номером нет")
            continue
        field = form["fields"][idx - 1]
        src = m.group(2).strip()
        if src in SOURCE_KEYS or src == "skip":
            mapping[field["entry"]] = src
        elif src.startswith("option:") or src.startswith("static:"):
            value = src.split(":", 1)[1].strip()
            if not value:
                errors.append(f"пустое значение у «{line[:40]}»")
            elif src.startswith("option:") and value not in field["options"]:
                errors.append(
                    f"{idx}: «{value}» нет среди вариантов поля "
                    f"({', '.join(field['options'][:5])})"
                )
            else:
                mapping[field["entry"]] = src
        else:
            errors.append(
                f"{idx}: источник «{src[:30]}» не знаю "
                "(wallet/handle/quote_url/comment_url/option:…/static:…/skip)"
            )
    if errors:
        raise GformError(";\n".join(errors))
    return validate_mapping(mapping, form)


def validate_mapping(mapping: dict, form: dict) -> dict[str, str]:
    """A {entry: source} dict from any surface (web/chat) → same gates.

    Every required field must be mapped to something that sends a value, and
    wallet must be mapped somewhere: the global dedup and the OK slot both
    key on the wallet, so a project that never sends one cannot be counted.
    """
    fields = {f["entry"]: f for f in form["fields"]}
    errors: list[str] = []
    clean: dict[str, str] = {}
    for entry, src in (mapping or {}).items():
        field = fields.get(str(entry))
        src = str(src or "").strip()
        if field is None:
            errors.append(f"неизвестное поле {entry}")
            continue
        if src in SOURCE_KEYS or src == "skip":
            # A field with fixed options (radio/dropdown/checkbox) only accepts
            # one of its own variants. Sending a wallet or a URL there is a
            # guaranteed HTTP 400 per row — measured on a live form where all
            # 145 rows came back «Отклонены проектом» exactly this way.
            if src != "skip" and field["options"]:
                errors.append(
                    f"«{field['title'][:40]}» — поле с вариантами, туда можно "
                    "только option:<вариант> или skip"
                )
                continue
            clean[field["entry"]] = src
        elif src.startswith("option:") or src.startswith("static:"):
            value = src.split(":", 1)[1].strip()
            if not value:
                errors.append(f"пустое значение у «{field['title'][:40]}»")
            elif src.startswith("option:") and value not in field["options"]:
                errors.append(
                    f"«{value}» нет среди вариантов «{field['title'][:40]}»"
                )
            elif src.startswith("static:") and field["options"]:
                errors.append(
                    f"«{field['title'][:40]}» — поле с вариантами, статический "
                    "текст туда не подходит (option:<вариант> или skip)"
                )
            else:
                clean[field["entry"]] = src
        else:
            errors.append(
                f"«{field['title'][:40]}»: источник «{src[:30]}» не знаю"
            )
    for field in form["fields"]:
        src = clean.get(field["entry"])
        if field["required"] and src in (None, "skip"):
            errors.append(
                f"обязательное поле «{field['title'][:40]}» не замаплено"
            )
    if "wallet" not in clean.values():
        # Say which: a form with no free-text field for a wallet cannot be
        # served at all — better one clear refusal than a created project
        # whose every row dies.
        text_fields = [f for f in form["fields"] if not f["options"]]
        if not text_fields:
            errors.append(
                "в форме нет ни одного текстового поля — кошелёк подставить "
                "некуда, конструктор такую форму не берёт"
            )
        else:
            errors.append(
                "wallet никуда не назначен — без него не считает слоты и дедуп"
            )
    if errors:
        raise GformError(";\n".join(errors))
    return clean


# --- step 3: build the project folder -------------------------------------------


def project_id_for(form: dict, owner: int) -> str:
    """Deterministic per (owner, form): re-adding the same form collides loudly
    instead of stacking a second project with a fresh limit."""
    h = hashlib.sha1(f"{int(owner)}:{form['form_id']}".encode()).hexdigest()[:8]
    return f"gf{h}"


MODULE_PY = '''"""Auto-generated Google Form adapter — built by the gform constructor. Regenerate, don't edit."""
from bot.gform import make_project

get_project = make_project(__file__)
'''


def render_fields(form: dict) -> str:
    """Numbered field list with pre-filled defaults — the admin edits numbers."""
    lines = [f"<b>{esc(form['title'] or form['form_id'])}</b>"]
    for i, f in enumerate(form["fields"], 1):
        req = " <b>*</b>" if f["required"] else ""
        lines.append(f"{i}. {esc(f['title'][:60])}{req} → <code>{esc(suggest_source(f))}</code>")
        if f["options"]:
            lines.append(f"   варианты: {esc(', '.join(f['options'][:6]))}")
    lines.append("")
    lines.append(
        "Ответь строками <code>N=источник</code>: wallet / handle / quote_url / "
        "comment_url / option:Да / static:текст / skip (по умолчанию skip).\n"
        "<b>*</b> — обязательное поле."
    )
    return "\n".join(lines)


def _user_root() -> Path:
    from bot import config

    return Path(config.USER_PROJECTS_DIR)


def form_folder(pid: str) -> Path | None:
    """A gform folder by id, in state first (user forms), then the checkout."""
    for root in (_user_root(), ROOT / "projects"):
        folder = root / pid
        if (folder / "form.json").is_file():
            return folder
    return None


def _read_def(folder: Path) -> dict:
    return json.loads((folder / "form.json").read_text(encoding="utf-8"))


def load_def(pid: str) -> dict | None:
    folder = form_folder(pid)
    if folder is None:
        return None
    try:
        return _read_def(folder)
    except (OSError, ValueError):
        return None


def user_forms(uid: int) -> list[dict]:
    """The user's own generated forms, oldest first."""
    root = _user_root()
    out = []
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        try:
            d = _read_def(folder)
        except (OSError, ValueError):
            continue
        if d.get("owner") == int(uid):
            out.append({
                "id": folder.name,
                "name": d.get("name") or folder.name,
                "url": d.get("url") or "",
                "public": bool(d.get("public")),
                "title": d.get("title") or "",
            })
    return out


def clean_name(raw: str, fallback: str) -> str:
    """Project name safe for toml and screens; user text wins over the form
    title (a form called «Free Mint» is not a project name the user wants)."""
    name = re.sub(r'["\\\n\r\t]', "", str(raw or "")).strip()
    return (name[:60] or fallback)


def clean_link(raw: str) -> tuple[str, str]:
    """(link, twitter_url) — an x.com/twitter link becomes the X button,
    anything else http(s) becomes the site button. Anything else is dropped."""
    raw = str(raw or "").strip()
    if not raw.startswith(("http://", "https://")) or len(raw) > 200:
        return "", ""
    host = urlparse(raw).netloc.lower().removeprefix("www.")
    if host in ("x.com", "twitter.com"):
        return "", raw
    return raw, ""


def build_project(
    form: dict,
    mapping: dict[str, str],
    owner: int,
    *,
    root: Path | None = None,
    enabled: bool = True,
    private: bool = True,
    name: str = "",
    link: str = "",
) -> tuple[str, Path]:
    """Write a gform folder. Returns (project_id, path).

    Operator drafts (/gform in chat): root=projects/, enabled=False — the two
    manual steps (smoke, flip) stay. User forms (web constructor): state dir,
    enabled=True, pinned to the creator until an admin publishes it.
    ``name``/``link`` are the user's own labels: the form's own title is a
    poor project name, and the form URL is not the project's home page.
    """
    from bot import config

    mapping = validate_mapping(mapping, form)
    pid = project_id_for(form, owner)
    target = Path(root) if root else _user_root()
    target.mkdir(parents=True, exist_ok=True)
    folder = target / pid
    if folder.exists():
        raise GformError("проект по этой форме уже существует")
    if not root and len(user_forms(owner)) >= int(config.MAX_USER_FORMS):
        raise GformError(
            f"у тебя уже {config.MAX_USER_FORMS} форм — удали одну, чтобы добавить новую"
        )
    name = clean_name(name, (form["title"] or pid))
    site_link, twitter_url = clean_link(link)
    folder.mkdir(parents=True)
    (folder / "__init__.py").touch()
    (folder / "module.py").write_text(MODULE_PY, encoding="utf-8")
    (folder / "form.json").write_text(
        json.dumps({"name": name, "owner": int(owner), "public": not private,
                    **form, "mapping": mapping},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    pin = f"\nonly_user_ids = {int(owner)}   # только создатель, пока админ не опубликует\n" if private else ""
    (folder / "project.toml").write_text(f'''# Generated by the gform constructor from {form["url"]}
id = "{pid}"
name = "{name}"
enabled = {str(enabled).lower()}
order = 60

# The form itself — «Заявка». The project's own home page / X only when the
# creator gave one; a Google-Forms link is not a project link.
link = "{site_link}"
twitter_url = "{twitter_url}"
{pin}''', encoding="utf-8")

    # Import gate: the folder must load exactly like a hand-written one
    # before anyone is told it exists.
    from bot.registry import load_project_folder

    project = load_project_folder(pid, projects_dir=folder.parent)
    if project is None:
        raise GformError("черновик не импортируется — смотрите лог")
    return pid, folder


def delete_form(uid: int, pid: str, *, cancel_jobs=None) -> Path:
    """Delete the user's own form folder. Submissions/dedup history stay in DB."""
    folder = form_folder(pid)
    if folder is None or folder.parent != _user_root():
        raise GformError("форма не найдена")
    d = _read_def(folder)
    if d.get("owner") != int(uid):
        raise GformError("это не твоя форма")
    if callable(cancel_jobs):
        cancel_jobs(pid)
    shutil.rmtree(folder)
    return folder


def set_public(pid: str, public: bool) -> dict:
    """Admin decision: publish (drop the creator pin) or keep private.

    The `public` flag in form.json is the record; the pin in project.toml is
    what the registry actually enforces, so both move together.
    """
    folder = form_folder(pid)
    if folder is None:
        raise GformError("форма не найдена")
    d = _read_def(folder)
    toml_path = folder / "project.toml"
    toml = toml_path.read_text(encoding="utf-8")
    if public:
        toml = re.sub(r"\nonly_user_ids = \d+[^\n]*\n", "\n", toml)
    else:
        toml = toml.rstrip("\n") + f"\nonly_user_ids = {int(d.get('owner') or 0)}\n"
    toml_path.write_text(toml, encoding="utf-8")
    d["public"] = bool(public)
    (folder / "form.json").write_text(
        json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


# One «Распарсить» per window per user: the button costs an outbound GET to
# docs.google.com, and an unmetered one is a proxy-scanner pointed at other
# people's forms.
PARSE_COOLDOWN_S = 30.0
_parse_last: dict[int, float] = {}
_parse_lock = threading.Lock()


def check_parse_cooldown(uid: int) -> float:
    """Seconds left on the cooldown, 0 = go."""
    now = time.monotonic()
    with _parse_lock:
        last = _parse_last.get(int(uid))
        if last is None or now - last >= PARSE_COOLDOWN_S:
            _parse_last[int(uid)] = now
            return 0.0
        return PARSE_COOLDOWN_S - (now - last)


# The parsed form the screen is showing, per user, short-lived. «Создать» must
# not re-fetch (the cooldown would refuse it) and must not trust the browser's
# copy of the fields either — the server keeps what it parsed and the button
# only says which source goes on which entry.
FORM_CACHE_TTL = 600.0
_form_cache: dict[int, tuple[float, dict]] = {}
_form_cache_lock = threading.Lock()


def cache_form(uid: int, form: dict) -> None:
    with _form_cache_lock:
        _form_cache[int(uid)] = (time.monotonic(), form)


def pop_form(uid: int) -> dict | None:
    with _form_cache_lock:
        item = _form_cache.pop(int(uid), None)
    if not item:
        return None
    stamp, form = item
    if time.monotonic() - stamp > FORM_CACHE_TTL:
        return None
    return form


# --- the runtime the generated module delegates to -------------------------------


def _load_def(module_file: str) -> dict:
    return json.loads(
        (Path(module_file).parent / "form.json").read_text(encoding="utf-8")
    )


def _row_value(source: str, fields: dict) -> str:
    if source == "wallet":
        return fields.get("wallet") or ""
    if source == "handle":
        return fields.get("handle") or ""
    if source in ("quote_url", "comment_url"):
        given = (fields.get(source) or "").strip()
        if given:
            return given
        return generate_status_url(fields.get("handle") or "")
    if source.startswith(("option:", "static:")):
        return source.split(":", 1)[1]
    return ""


def _post_url(form_url: str) -> str:
    return re.sub(r"/(viewform|edit)(\?.*)?$", "/formResponse", form_url)


def submit(
    form_def: dict,
    fields: dict,
    *,
    proxies: list[str],
    proxy_idx: int,
    retries: int = 4,
    retry_delay: float = 3.0,
    min_delay: float = 0.3,
    max_delay: float = 1.2,
    **_extra,
) -> dict:
    handle = fields.get("handle") or ""
    wallet = fields.get("wallet") or ""
    base = {"handle": handle, "wallet": wallet}
    proxy_pool = proxies or []
    if not proxy_pool:
        return no_proxies_result(
            handle=handle, wallet=wallet,
            extra={"detail": "google form requires a proxy"},
        )

    if max_delay and max_delay > 0:
        time.sleep(random.uniform(max(0.0, min_delay), max_delay))

    url = _post_url(form_def["url"])
    body = {"fvv": "1", "pageHistory": "0"}
    for entry, source in (form_def.get("mapping") or {}).items():
        if source == "skip":
            continue
        value = _row_value(source, fields)
        if value:
            body[entry] = value

    headers, _ = random_fingerprint("https://docs.google.com", form_def["url"], url=url)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://docs.google.com",
        "Referer": form_def["url"],
    })

    attempts = max(1, int(retries))
    last_detail = ""
    with requests.Session() as session:
        for attempt in range(attempts):
            proxy_url = choose_proxy_url(proxy_pool, proxy_idx, attempt)
            try:
                r = session.post(
                    url, headers=headers, data=body,
                    proxies=proxy_dict_for(proxy_url), timeout=TIMEOUT,
                    allow_redirects=False,
                )
                raw = " ".join((r.text or "").split())[:500]
                if r.status_code == 200:
                    if is_google_form_closed(r):
                        return {"status": "BAD_RESP",
                                "detail": "google form: closed / no longer accepting",
                                **base}
                    if RECORDED_MARKER in (r.text or ""):
                        return {"status": "OK",
                                "detail": "google form: response recorded",
                                **base}
                    return {"status": "OK",
                            "detail": f"HTTP 200 (no marker): {raw[:200]}",
                            **base}
                if r.status_code == 401:
                    _disable_login_walled(form_def.get("pid"), form_def.get("name") or "")
                    return {"status": "BAD_RESP",
                            "detail": "google form: 401 — форма требует вход в Google",
                            **base}
                last_detail = f"HTTP {r.status_code}: {raw}"
                if 400 <= r.status_code < 500:
                    return {"status": "BAD_RESP", "detail": last_detail, **base}
                if attempt + 1 < attempts:
                    time.sleep(backoff_sleep(attempt, retry_delay, last_detail))
                    continue
                return {"status": "FAIL", "detail": last_detail, **base}
            except Exception as exc:  # noqa: BLE001 — network/proxy shapes vary
                last_detail = format_request_error(exc, error_proxy_label(proxy_url))
                if attempt + 1 < attempts:
                    time.sleep(backoff_sleep(attempt, retry_delay, last_detail))
                    continue
    return {"status": "FAIL", "detail": last_detail or "no answer from form", **base}


def _disable_login_walled(pid: str | None, name: str = "") -> None:
    """Runtime-disable a form whose formResponse answers 401.

    A login-walled form is permanent (§3.1): Google serves the page anonymously
    but refuses every POST, with or without headers. gftestform burned 700 rows
    into that answer with no alert and no disable, because the 401 detail is not
    a closed-form phrase. So the adapter acts itself instead of waiting for an
    admin tap: runtime flag off, its jobs soft-cancelled — exactly what the
    admin «hide project» button would have done.
    """
    if not pid:
        return
    try:
        from bot import db as bot_db
        from bot.submit_queue import cancel_jobs_for_project

        if bot_db.is_project_runtime_disabled(pid):
            return
        bot_db.set_project_runtime_disabled(pid, True, admin_id=0)
        cancel_jobs_for_project(pid)
        print(
            f"[gform] {pid} ({name or pid}) login-walled (401) — "
            "runtime-disabled, jobs cancelled",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 — never fail the row over this
        print(f"[gform] disable {pid} failed: {exc}", flush=True)


def make_project(module_file: str):
    """get_project() for the generated module.py (registry dict contract)."""
    form_def = _load_def(module_file)
    pid = Path(module_file).parent.name
    form_def.setdefault("pid", pid)
    name = form_def.get("name") or pid
    mapping = form_def.get("mapping") or {}
    has_handle = "handle" in mapping.values()
    url_keys = [k for k in ("comment_url", "quote_url") if k in mapping.values()]

    bulk_columns = [("handle", has_handle, "twitter handle"),
                    ("wallet", True, "wallet 0x...")]
    bulk_columns += [(k, False, f"{k} url") for k in url_keys]

    def help_html() -> str:
        return (
            f"Формат: <code>handle|0xYourWallet</code> (генератор из /gform).\n"
            f"Форма: <a href=\"{esc(form_def['url'])}\">{esc(name)}</a>"
        )

    def parse(text: str) -> dict | None:
        value = (text or "").strip().lstrip("\ufeff")
        if not value or value.startswith("#"):
            return None
        parts = [p.strip() for p in re.split(r"[|,\t]+", value) if p.strip()]
        wallet = next((p for p in parts if is_valid_wallet(p)), "")
        handle = next(
            (normalize_twitter(p, max_len=15) for p in parts
             if p != wallet and normalize_twitter(p, max_len=15)),
            "",
        )
        if not wallet:
            return None
        return {"handle": handle, "wallet": wallet.lower()}

    def validate(fields: dict) -> str | None:
        if not is_valid_wallet(fields.get("wallet") or ""):
            return "Некорректный wallet. Нужен <code>0x</code> + 40 hex."
        fields["wallet"] = fields["wallet"].strip().lower()
        if has_handle and not fields.get("handle"):
            return "Некорректный handle. Буквы, цифры, _ — до 15 символов."
        return None

    def do_submit(fields, **kw) -> dict:
        return submit(form_def, fields, **kw)

    def format_result(result: dict, fields: dict, **_extra) -> str:
        status = (result or {}).get("status", "FAIL")
        h = esc(fields.get("handle") or "—")
        w = esc(fields.get("wallet") or "—")
        if status == "OK":
            return (f"✅ <b>OK</b> · {esc(name)}\n@{h} · <code>{w}</code>\n"
                    "Ответ записан в форму. Слот списан.")
        if status == "ALREADY":
            return (f"ℹ️ <b>Уже в списке</b> · {esc(name)}\n@{h} · <code>{w}</code>\n"
                    "Слот не списан.")
        detail = str((result or {}).get("detail") or "")
        label = "Форма не приняла" if status == "BAD_RESP" else "FAIL (сеть/прокси)"
        return (f"⚠️ <b>{label}</b> · {esc(name)}\n@{h} · <code>{w}</code>\n"
                f"Слот не списан.\n<code>{esc(detail[:200])}</code>")

    def get_project() -> dict:
        return {
            "id": pid,
            "name": name,
            # Never the gate (§16.7): the tier ladder decides. High default so
            # no screen reads "limit 30" over a VIP's 1000.
            "ok_limit": 1000,
            "help_html": help_html,
            "parse": parse,
            "validate": validate,
            "submit": do_submit,
            "format_result": format_result,
            "allow_direct": False,
            "prefer_direct": False,
            "min_delay": 0.3,
            "max_delay": 1.2,
            "retries": 4,
            "retry_delay": 3.0,
            "bulk_columns": bulk_columns,
        }

    return get_project
