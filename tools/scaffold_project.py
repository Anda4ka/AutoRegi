#!/usr/bin/env python3
"""Read a project's site and report where its registration form actually posts.

Adding an adapter is mostly reconnaissance: fetch the page, find the bundle,
read the handler, work out the endpoint and the field names, then write two
hundred lines that are largely the same as the last project's. This automates
the first half and refuses to guess at the second.

    python tools/scaffold_project.py https://demo-project.xyz
    python tools/scaffold_project.py https://demo-project.xyz --id demo-project --write

**Every finding carries its evidence** — the file it came from and the text
around it — because a scaffolder that reports a plausible endpoint without one
is the same failure as a model citing a tweet it never opened. What it cannot
determine it says it cannot determine; the operator reads the snippet and
decides. Nothing here contacts the endpoint: discovery must not create rows.

Measured against the six adapters in this repo whose submit URL is a named
constant: **four of six**. The two misses are sites that build the path at
runtime or hide the form behind a click. Following links to `/apply`-style
pages and catching relative `"/api/..."` paths is what took it from one to
four — the landing page is rarely the form, and bundles almost never write the
absolute URL.

So: a hit saves the slowest part of adding a project. A miss costs one command
and tells you which files it already read, which is worth something too.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
# Next.js pulls 30+ chunks per page; 25 with a length sort cut the *route*
# chunk (its path is the longest) on tyros.store — exactly the file the
# submit handler lives in. Route chunks go first now, and the cap rose.
MAX_ASSETS = 40
SNIPPET = 160

# Assets that are never the app's own code. Both false captcha keys and the
# etherscan "endpoint" measured on 2026-09-08 came from vendor blobs
# (jsdelivr ethers, a viem chains table bundled into a Next chunk); a CDN
# library cannot hold the form handler, only noise.
VENDOR_HOSTS = (
    "cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "esm.sh",
    "cdn.tailwindcss.com", "www.gstatic.com", "cdn.skypack.dev",
    "fonts.googleapis.com", "fonts.gstatic.com", "cdn.socket.io",
)
# Third-party APIs a web3 bundle always mentions and a whitelist form never
# posts to. Seen live as false `own_api` hits on tyros.store.
EXCLUDE_ENDPOINT_HOSTS = (
    "api.etherscan.io", "etherscan.io", "sentry.io", "o1.ingest.sentry.io",
    "api.infura.io", "infura.io", "alchemy.com", "walletconnect.org",
    "registry.walletconnect.com", "cloudflare-eth.com", "rpc.ankr.com",
    "api.coingecko.com", "docs.google.com", "accounts.google.com",
)

# Platform → which adapter solves this shape. The public repo ships only the
# starter; point each entry at your own adapter once you have one (a template
# that is not on disk cannot be copied — --init dies on it).
TEMPLATES = {
    "supabase_rest": ("", "вставка строки в Supabase PostgREST (RPC — тот же REST)"),
    "apps_script": ("", "Google Apps Script sheet (JSON text/plain, echo verdict)"),
    "own_api": ("projects/_template/module.py", "POST JSON на свой бэкенд"),
    "own_api_relative": ("projects/_template/module.py", "POST JSON на свой бэкенд"),
    "firebase_firestore": ("", "Firestore addDoc через REST documents:commit"),
    "next_action": ("", "Next.js server action"),
    "netlify_forms": ("", "Netlify Forms (капча — см. bot/captcha.py)"),
    # No template on purpose: a Google Form with verified-email collection
    # answers 401 on formResponse — that is a refusal class, not an adapter
    # (PROJECTS.md, «API cookbook»).
    "google_form": ("", "Google Form — проверить FB_PUBLIC_LOAD_DATA_[1][10][1]"),
}

PATTERNS = {
    "supabase_rest": r"https://[a-z0-9]+\.supabase\.co/rest/v1/[\w/-]+",
    "apps_script": r"https://script\.google\.com/macros/s/[\w-]+/exec",
    "google_form": r"https://docs\.google\.com/forms/[^\"'\s]+/formResponse",
    "own_api": r"[\"'`](https?://[^\"'`\s]+/(?:api|submit|register|apply|join|"
               r"whitelist|wl|drops|early)(?:/[\w-]+)*)[\"'`]",
    # Relative paths are the common case and the one that was missed: a bundle
    # writes `fetch("/api/apply")`, not the absolute URL. Measured against six
    # adapters with known endpoints, adding this took the hit rate from 1 to 5.
    "own_api_relative": r"[\"'`](/(?:api|submit|register|apply|join|whitelist|wl"
                         r"|drops|early)(?:/[\w-]+)*)[\"'`]",
}

# Firebase web SDK: the page writes with addDoc, the REST shape is a commit
# (documents/create rejects serverValue in a field — use documents:commit + updateTransforms).
FIREBASE_CONFIG_RE = re.compile(r'projectId["\']?\s*[:=]\s*["\']([a-z0-9-]+)["\']', re.I)
FIREBASE_COLLECTION_RE = re.compile(r'collection\(\s*\w+\s*,\s*["\']([\w-]+)["\']')
FIREBASE_ADDDOC_RE = re.compile(r'addDoc\s*\(')
# Next.js server actions surface in bundles as a reference id.
NEXT_ACTION_RE = re.compile(r'createServerReference\)\(\s*["\']([0-9a-f]{40,})["\']')
# Netlify forms declare themselves in markup.
NETFLIFY_RE = re.compile(r'data-netlify|name=["\']form-name["\']|/\.netlify/functions/', re.I)

# A captcha key regex alone is not evidence: both "recaptcha" keys the scout
# reported on 2026-09-08 were random base58 from ethers/viem bundles. Require
# the page to actually load a captcha script before claiming one.
CAPTCHA_SCRIPT_RE = re.compile(
    r"challenges\.cloudflare\.com/turnstile|recaptcha/api\.js|hcaptcha\.com/1/api\.js", re.I)
CAPTCHA = {
    "turnstile": r"(0x4AAA[A-Za-z0-9_-]{10,})",
    "recaptcha": r"(6L[A-Za-z0-9_-]{20,})",
}

# Pages a form actually lives on. The root is usually a landing page — the
# apply form is at /apply and nothing about the endpoint appears on / at all.
FORM_PATHS = ("/apply", "/whitelist", "/wl", "/register", "/join", "/mint", "/form")
FORM_LINK_RE = re.compile(r'href=["\']([^"\']*(?:apply|whitelist|register|join|wl)[^"\']*)["\']',
                          re.I)

def fetch(url: str) -> tuple[str, str]:
    """(text, error). Never raises: a dead asset must not stop the sweep."""
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
    except requests.RequestException as exc:
        return "", f"{type(exc).__name__}"
    if r.status_code != 200:
        return "", f"HTTP {r.status_code}"
    return r.text or "", ""


def assets_of(html: str, base: str) -> list[str]:
    """Script and module URLs the page pulls in; route chunks and same-origin first."""
    found = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I)
    found += re.findall(r'<link[^>]+rel=["\']modulepreload["\'][^>]+href=["\']([^"\']+)["\']',
                        html, re.I)
    origin = urlparse(base).netloc
    urls, seen = [], set()
    for src in found:
        full = urljoin(base, src)
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)

    def rank(u: str) -> tuple:
        host = urlparse(u).netloc
        if host != origin:
            return (9, len(u))  # vendor/CDN: fetched last, below the cap
        # The route chunk (`app/.../page-*.js`) is where the submit handler
        # lives; the layout chunk carries auth plumbing, and a length sort
        # alone kept putting it first on big Next.js pages.
        if "/app/" in u:
            return (0 if "/page" in u else 1, len(u))
        return (2, len(u))

    urls.sort(key=rank)
    return urls[:MAX_ASSETS]


def form_pages(html: str, base: str) -> list[str]:
    """Same-origin pages likely to hold the form, links first then guesses."""
    origin = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    out, seen = [], {base.rstrip("/")}
    for href in FORM_LINK_RE.findall(html):
        full = urljoin(base, href).split("#")[0].rstrip("/")
        if urlparse(full).netloc == urlparse(base).netloc and full not in seen:
            seen.add(full)
            out.append(full)
    for path in FORM_PATHS:
        full = origin + path
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out[:8]


def around(text: str, index: int, width: int = SNIPPET) -> str:
    lo = max(0, index - width // 2)
    return re.sub(r"\s+", " ", text[lo:lo + width]).strip()


def scan(sources: dict[str, str], base: str = "") -> dict:
    """Look for endpoints, captcha and field names across every fetched file."""
    endpoints: dict[str, list[dict]] = {}
    for kind, pattern in PATTERNS.items():
        for origin, text in sources.items():
            for m in re.finditer(pattern, text):
                url = m.group(1) if m.groups() else m.group(0)
                raw = url
                host = urlparse(url if url.startswith("http") else f"https://{urlparse(base).netloc}{url}").netloc
                if host in EXCLUDE_ENDPOINT_HOSTS:
                    continue
                if any(raw == e.get("raw") for e in endpoints.get(kind, [])):
                    continue
                shown = url
                if kind == "own_api_relative":
                    # Resolve against the site, not the asset: a bundle served
                    # from a CDN would otherwise point the endpoint at the CDN.
                    site = urlparse(base or origin)
                    shown = f"{site.scheme}://{site.netloc}{url}"
                    if any(shown == e["url"] for e in endpoints.get("own_api", [])):
                        continue
                endpoints.setdefault(kind, []).append({
                    "url": shown, "raw": raw, "found_in": origin,
                    "near": around(text, m.start()),
                })

    # Nav links (`/drops/<slug>/vault`) and reader/auth plumbing drown the
    # submit handler beyond the four printed items. Rank: a path that starts
    # with an API verb beats `/drops/<slug>` links, and a POST beside the
    # match beats a bare GET.
    for items in endpoints.values():
        items.sort(key=lambda e: (
            0 if re.match(r"^https?://[^/]+/(api|submit|register|apply|join|whitelist|wl)(/|$)",
                          e["url"]) else 1,
            0 if '"POST"' in e.get("near", "") or "method:" in e.get("near", "") else 1,
        ))

    # Firebase Firestore: addDoc in the page's own code + a config with a
    # projectId. The endpoint is assembled, so no URL pattern can catch it.
    for origin, text in sources.items():
        cfg = FIREBASE_CONFIG_RE.search(text)
        if not cfg or not FIREBASE_ADDDOC_RE.search(text):
            continue
        coll = FIREBASE_COLLECTION_RE.search(text)
        project_id = cfg.group(1)
        endpoints.setdefault("firebase_firestore", []).append({
            "url": (f"https://firestore.googleapis.com/v1/projects/{project_id}"
                    "/databases/(default)/documents/"
                    + (f"{coll.group(1)}" if coll else "<collection>")),
            "found_in": origin,
            "near": around(text, cfg.start()),
        })
        break

    # Next.js server actions and Netlify forms carry no URL at all.
    for origin, text in sources.items():
        m = NEXT_ACTION_RE.search(text)
        if m:
            endpoints.setdefault("next_action", []).append({
                "url": f"(server action id {m.group(1)[:16]}…)",
                "found_in": origin, "near": around(text, m.start()),
            })
            break
    for origin, text in sources.items():
        if NETFLIFY_RE.search(text):
            endpoints.setdefault("netlify_forms", []).append({
                "url": "(markup form → POST / with form-name)",
                "found_in": origin,
                "near": around(text, NETFLIFY_RE.search(text).start()),
            })
            break

    captcha = {}
    script_seen = any(CAPTCHA_SCRIPT_RE.search(text) for text in sources.values())
    if script_seen:
        # Only now is a key worth reporting: without the loader script a
        # "key" is random base58 from a web3 bundle (two false hits on
        # 2026-09-08, both from ethers/viem).
        for kind, pattern in CAPTCHA.items():
            for origin, text in sources.items():
                m = re.search(pattern, text)
                if m:
                    captcha[kind] = {"key": m.group(1), "found_in": origin,
                                     "near": around(text, m.start())}
                    break

    # Field names are read from the request body beside a discovered endpoint,
    # not from the whole bundle. Scanning everything found `useRef`,
    # `hrefLang` and `refCount` from React's internals and buried the one real
    # field under them — noise that reads like a finding is worse than none.
    fields: dict[str, dict] = {}
    for items in endpoints.values():
        for entry in items:
            text = sources.get(entry["found_in"], "")
            at = text.find(entry["url"])
            if at < 0:
                continue
            window = text[at:at + 700]
            body = re.search(r"(?:JSON\.stringify|body\s*:)\s*\(?\{(.{0,400}?)\}",
                             window, re.S)
            scope = body.group(1) if body else window
            for m in re.finditer(r"[{,(]?\s*[\"']?([A-Za-z_][\w]{1,24})[\"']?\s*:", scope):
                name = m.group(1)
                if name.lower() in fields or name in ("method", "headers", "body"):
                    continue
                fields[name.lower()] = {
                    "name": name, "found_in": entry["found_in"],
                    "near": re.sub(r"\s+", " ", scope)[:120].strip(),
                    "from_body": bool(body),
                }

    entries = sorted({m.group(0) for origin, text in sources.items()
                      for m in re.finditer(r"entry\.\d{6,}", text)})
    return {"endpoints": endpoints, "captcha": captcha,
            "fields": list(fields.values()), "form_entries": entries}


def report(url: str, found: dict, sources: dict[str, str], errors: list[str]) -> None:
    print(f"\n=== {url}")
    print(f"прочитано файлов: {len(sources)}"
          + (f", не открылось: {len(errors)}" if errors else ""))

    if not found["endpoints"]:
        print("\nЭНДПОИНТОВ НЕ НАЙДЕНО.")
        print("  Это обычный результат, а не сбой: адрес может собираться из")
        print("  кусков, лежать в чанке, который грузится по клику, или форма")
        print("  живёт на странице, до которой не дошли по ссылкам.")
        print("\n  Уже прочитано — повторять не нужно:")
        for src in list(sources)[:12]:
            print(f"    {src}")
        if len(sources) > 12:
            print(f"    …и ещё {len(sources) - 12}")
        print("\n  Дальше: DevTools → Network, отправить форму руками и")
        print("  посмотреть, какой запрос ушёл.")
    for kind, items in found["endpoints"].items():
        template, human = TEMPLATES.get(kind, ("—", kind))
        print(f"\n[{kind}] {human}")
        print(f"  копировать из: {template}")
        for e in items[:4]:
            print(f"  → {e['url']}")
            print(f"     найдено в: {e['found_in']}")
            print(f"     рядом:     …{e['near']}…")

    if found["captcha"]:
        print("\nКАПЧА:")
        for kind, info in found["captcha"].items():
            print(f"  {kind}: {info['key']}")
            print(f"     найдено в: {info['found_in']}")
        print("  Решается через bot/captcha.py. Пустой токен слать НЕЛЬЗЯ —")
        print("  сайт примет и выбросит строку позже: PROJECTS.md §13.")

    if found["form_entries"]:
        print(f"\nGOOGLE FORM entry.*: {', '.join(found['form_entries'][:12])}")

    if found["fields"]:
        certain = [f for f in found["fields"] if f.get("from_body")]
        shown = certain or found["fields"]
        head = ("ПОЛЯ ЗАПРОСА (из тела рядом с эндпоинтом):" if certain
                else "ИМЕНА РЯДОМ С ЭНДПОИНТОМ (тело не распозналось):")
        print(f"\n{head}")
        for f in shown[:10]:
            print(f"  {f['name']}")
        if shown:
            print(f"  контекст: …{shown[0]['near']}…")
    else:
        print("\nИмён полей не видно — payload собирается не литералом.")

    print("\nЧТО ЭТО НЕ ГОВОРИТ:")
    print("  • какой ответ считать успехом, а какой — ALREADY;")
    print("  • обязателен ли handle и что уходит, когда он пуст;")
    print("  • есть ли серверная проверка задач (подписка, репост);")
    print("  • нужен ли ключ/заголовок авторизации.")
    print("  Это выясняется чтением найденного файла и живой пробой одного")
    print("  кошелька. Скрипт нарочно ничего никуда не отправлял.")


def write_draft(project_id: str, url: str, found: dict) -> Path:
    """A project.toml with what was actually established, and nothing else."""
    target = ROOT / "projects" / project_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "__init__.py").touch()

    kind = best_kind(found)
    endpoint = found["endpoints"].get(kind, [{}])[0].get("url", "")
    template = TEMPLATES.get(kind, ("—", ""))[0]
    fields = ", ".join(f["name"] for f in found["fields"][:8]) or "не определены"

    toml = target / "project.toml"
    if toml.exists():
        return toml
    toml.write_text(f'''# ЧЕРНОВИК, собран tools/scaffold_project.py — проверьте каждую строку.
# Заполнено только то, что действительно найдено на странице; остальное
# оставлено пустым намеренно, а не забыто.
id = "{project_id}"
name = "{project_id.title()}"
enabled = false          # включать после живого теста на собственных кошельках
order = 999              # выше = новее; список сортируется по убыванию

link = "{url}"           # форма заявки — кнопка «Заявка»
twitter_url = ""         # ОБЯЗАТЕЛЬНО: без него сканер минтов проект не увидит
mint_info = ""           # строка на экране проекта, пока нет разобранных фаз

# Найдено скриптом:
#   тип API:   {kind or "не определён"}
#   эндпоинт:  {endpoint or "не найден"}
#   копировать: {template}
#   поля:      {fields}
''', encoding="utf-8")
    return toml


def _rewrite_const(text: str, name: str, value: str) -> str:
    """Replace a module-level `NAME = "..."` constant, whatever its old value."""
    return re.sub(rf'^{name} = "[^"]*"$', f'{name} = "{value}"', text,
                  count=1, flags=re.M)


# Most specific first: dict order of `endpoints` follows PATTERNS, so a nav
# link matched by own_api_relative must not outrank a detected platform.
KIND_PRIORITY = (
    "firebase_firestore", "apps_script", "supabase_rest", "google_form",
    "next_action", "netlify_forms", "own_api", "own_api_relative",
)


def best_kind(found: dict) -> str:
    for kind in KIND_PRIORITY:
        if found["endpoints"].get(kind):
            return kind
    return next(iter(found["endpoints"]), "")


def init_project(project_id: str, url: str, found: dict) -> tuple[Path, list[str]]:
    """Scaffold `projects/<id>/` from the matching live template.

    A draft, not an adapter: the endpoint constants are substituted where a
    plain string swap can do it, and everything the scout could not know
    (success wording, ALREADY shape, field names) stays for the author. The
    returned checklist is the honest list of what is still fake.
    """
    target = ROOT / "projects" / project_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "__init__.py").touch()

    kind = best_kind(found)
    template_rel = TEMPLATES.get(kind, ("", ""))[0]
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    todo: list[str] = []

    toml = target / "project.toml"
    if not toml.exists():
        endpoint = found["endpoints"].get(kind, [{}])[0].get("url", "")
        fields = ", ".join(f["name"] for f in found["fields"][:8]) or "не определены"
        toml.write_text(f'''# ЧЕРНОВИК, собран tools/scaffold_project.py --init — проверьте каждую строку.
id = "{project_id}"
name = "{project_id.title()}"
enabled = false          # включать после живого теста на собственных кошельках
order = 60

link = "{url}"           # форма заявки — кнопка «Заявка»
twitter_url = ""         # ОБЯЗАТЕЛЬНО: без него сканер минтов проект не увидит
mint_info = ""           # строка на экране проекта, пока нет разобранных фаз

# Найдено скриптом:
#   тип API:   {kind or "не определён"}
#   эндпоинт:  {endpoint or "не найден"}
#   шаблон:    {template_rel or "нет живого шаблона — см. PROJECTS.md, поваренная книга"}
#   поля:      {fields}
''', encoding="utf-8")

    if not template_rel:
        todo.append("нет живого шаблона для этого типа API — писать с нуля (PROJECTS.md, поваренная книга)")
        return toml, todo
    template_api = ROOT / template_rel if template_rel else None
    if template_api is None or not template_api.exists():
        todo.append(f"шаблон {template_rel or '—'} не найден на диске — скопируй ближайший из projects/")
        return toml, todo
    template_dir = template_api.parent

    for fname in ("api.py", "module.py"):
        text = (template_dir / fname).read_text(encoding="utf-8")
        if fname == "api.py":
            text = _rewrite_const(text, "SITE_ORIGIN", origin)
            text = _rewrite_const(text, "PAGE_URL", url.rstrip("/"))
            text = _rewrite_const(text, "SITE_REFERER", url.rstrip("/"))
        else:
            text = _rewrite_const(text, "ID", project_id)
            text = _rewrite_const(text, "NAME", project_id.title())
        out = target / fname
        if not out.exists():
            out.write_text(text, encoding="utf-8")
    todo.extend([
        f"api.py: подставить настоящий эндпоинт и поля ({kind})",
        "api.py: определить успех и ALREADY по живому ответу",
        "module.py: parse/validate/help под формат проекта",
        "project.toml: twitter_url, mint_info; потом order",
    ])
    return toml, todo


def gather(url: str) -> tuple[dict, dict[str, str], list[str]]:
    """Fetch the page, its form pages and bundles; return (found, sources, errors).

    Split out of main() so the suggest pipeline can run recon in-process
    without a subprocess and without argparse.
    """
    html, err = fetch(url)
    if not html:
        raise SystemExit(f"страница не открылась: {err}")

    sources = {url: html}
    errors: list[str] = []
    pages = [(url, html)]
    for candidate in form_pages(html, url):
        text, page_err = fetch(candidate)
        if text and candidate not in sources:
            sources[candidate] = text
            pages.append((candidate, text))
        elif page_err:
            errors.append(f"{candidate}: {page_err}")

    seen_assets: set[str] = set()
    for page_url, page_html in pages:
        for asset in assets_of(page_html, page_url):
            if asset in seen_assets or len(seen_assets) >= MAX_ASSETS:
                continue
            # A CDN library cannot hold the form handler; not fetching it
            # saves the request and the noise in one move.
            if urlparse(asset).netloc in VENDOR_HOSTS:
                continue
            seen_assets.add(asset)
            text, asset_err = fetch(asset)
            if text:
                sources[asset] = text
            else:
                errors.append(f"{asset}: {asset_err}")

    return scan(sources, url), sources, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--id", default="", help="id проекта; с --write/--init создаёт папку")
    ap.add_argument("--write", action="store_true", help="записать черновик project.toml")
    ap.add_argument("--init", action="store_true",
                    help="создать projects/<id>/ целиком: toml + api.py + module.py из шаблона")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    args = ap.parse_args()

    found, sources, errors = gather(args.url)
    if args.json:
        print(json.dumps({"url": args.url, **found, "read": list(sources)},
                         ensure_ascii=False, indent=2))
        return 0

    report(args.url, found, sources, errors)
    if args.write or args.init:
        if not args.id:
            print("\n--write/--init требует --id")
            return 2
        if args.init:
            path, todo = init_project(args.id, args.url, found)
            print(f"\nчерновик: {path}")
            for line in todo:
                print(f"дальше:   {line}")
        else:
            path = write_draft(args.id, args.url, found)
            print(f"\nчерновик: {path}")
            print(f"дальше:   cp {TEMPLATES.get(best_kind(found), ('…',))[0]}"
                  f" projects/{args.id}/api.py  и править под найденный эндпоинт")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
