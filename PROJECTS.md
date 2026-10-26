# PROJECTS.md — adding, updating and removing projects

The contract between the bot core and one whitelist site. The core never
imports a project by name — everything is discovered in `projects/`.

```
projects/<id>/            # folder name = stable project id (DB, callbacks)
  project.toml            # metadata, links, limits, health probe
  module.py               # parse / validate / submit adapter
  api.py                  # (optional) HTTP for the site
```

The registry (`bot/registry.py`) scans `projects/` (plus user-generated
`$STATE_DIR/user_projects/` from the Mini App constructor) on boot, `/reload`
and a 30s source watcher. A folder starting with `_` or without
`project.toml` is skipped; `enabled = false` hides the project. A module that
fails to import is logged and skipped (strict mode: `PROJECTS_STRICT=1`).

---

## 1. `project.toml`

```toml
id = "myproject"            # REQUIRED, must equal the folder name
name = "My Project"         # button text
enabled = true              # false = not loaded
order = 40                  # button sort (lower = higher)
ok_limit = 30               # base per-user OK limit for this project
description = "internal note"   # optional, never shown in the TG UI

# Links (project screen + inline URL buttons)
twitter_url = "https://x.com/myproject"
link = "https://myproject.xyz"  # if it equals twitter_url → one X button

# Mint announcement line on the project screen (plain text, HTML-escaped)
mint_info = "18 Aug · 13:00 UTC · 444 pcs · 0.0016 ETH"
mint_info_en = "…"              # optional; RU is the fallback
mint_url = "https://myproject.xyz/mint"   # extra 🗓 button

# Optional runtime knobs
# health_url = "https://…"     # read-only probe for the admin health panel
# health_method = "GET"        # GET/HEAD/OPTIONS only
# auto_close_on_gate = true    # runtime-disable the moment the probe reads closed
# requires_captcha = true      # every row costs a paid solve → VIP-only
                               # unless the user set their own solver key
# only_user_ids = [123456789]  # pin the project to named Telegram ids
```

`ok_limit` is **not** the submit gate — the tier ladder in `bot/limits.py`
(`effective_ok_limit`) is. Changing only links never needs `module.py`
edits: toml + restart / `/reload`.

## 2. `module.py` — required API

The module must export `get_project()` (preferred) or a `project` object.

| Function | Signature | Behavior |
|----------|-----------|----------|
| `help_html()` | `() -> str` | HTML format help for the user |
| `parse(text)` | `(str) -> dict \| None` | `None` = "unknown format" |
| `validate(fields)` | `(dict) -> str \| None` | `None` = ok; else user-facing error |
| `submit(fields, **kwargs)` | `-> dict` | at minimum `status`, `detail` |
| `get_project()` | `() -> dict` | bundles all of the above |

`get_project()` extras: `bulk_columns` — `list[(key, required, label)]`,
required for `.txt` batch upload; `min_delay` / `max_delay` / `retries` /
`retry_delay` per-project HTTP pacing; `format_result` — custom result HTML.

`submit` kwargs passed by the bot (accept with `**`):
`proxies, proxy_idx, retries, retry_delay, min_delay, max_delay`.

Return statuses:

| status | meaning | burns a slot? |
|--------|---------|---------------|
| `OK` | server accepted a **new** row | **yes** |
| `ALREADY` | duplicate / HTTP 409 / unique-violation | no |
| `BAD_RESP` | other 4xx, business reject, bad body | no |
| `FAIL` | network / proxy / retries exhausted | no |
| `BUSY` | endpoint contended (locked sheet etc.) — only for a real lock message in `detail` | no |

Suggested `fields` keys: `handle` (X/Twitter nick), `wallet` (`0x…` + 40 hex),
plus site-specific extras.

### What a module must NOT do

- Register Telegram handlers, open its own limits DB or hardcode `proxy.txt`
  paths — the core passes proxies in.
- Log proxy credentials.
- Appear hardcoded in `tgbot.py` / `bot/*` — everything flows through the
  registry.

### Anti-patterns (each one cost a real run)

1. `BUSY` only for a genuine lock message in the response text — not for any
   `BAD_RESP`.
2. Permanent rejects (invalid handle, duplicate, 409) → `BAD_RESP`/`ALREADY`
   immediately, no retry loop, never `FAIL`.
3. Wallet regex: `0x` + 40 hex with a negative lookahead, or a tx hash gets
   truncated into a "wallet".
4. `parse`: `lstrip("@")` the handle.
5. Do not swallow `ok_limit = 0` / `order = 0` with `x or default`.

## 3. Adding a project — checklist

1. **Recon the site's API.** `python tools/scaffold_project.py https://site.xyz`
   reads the page, follows apply-like links, downloads JS bundles and prints
   candidate endpoints with the code snippet each came from, plus captcha
   keys and request fields. If it stays silent, capture the request in
   DevTools → Network (method, URL, body fields, the success response, the
   typical errors). Or run the whole checklist:
   `python tools/new_project.py <url> <id>` (recon → draft → dry-run →
   `--live` smoke on your own accounts; it never sets `enabled = true`).
2. **Write `api.py`** with a callable
   `submit(entry, proxies, proxy_idx, idx, total, retries, retry_delay, min_delay, max_delay, ...) -> dict`.
   Detect success exactly like the real API does — never invent it. Shared
   helpers live in `bot/http_client.py` (fingerprints, wallet validation,
   GAS-lock detection, proxy loading).
3. **Copy the folder:** `cp -a projects/_template projects/foo`, fill in
   `project.toml` and `module.py`.
4. **Registry check:**
   `python -c "from bot.registry import load_projects, list_projects; load_projects(); print([p.id for p in list_projects()])"`
5. **Live smoke test on accounts you own** — a small count, and verify the
   rows landed on the project's side, not only in your own database:
   `python tools/smoke_project.py foo --user <your_id> --count 10 --dry-run`
   then without `--dry-run`. An `OK` in our log is a claim, not a receipt.
6. Set `enabled = true`, restart the bot (or admin `/reload`).

### API cookbook

| Shape | Request | Gotchas |
|---|---|---|
| JSON POST | `POST {origin}/api/...` with JSON body | success = 2xx **and** the expected body object, not just 2xx; `409` → `ALREADY` |
| Supabase REST | `POST .../rest/v1/<table>`, `apikey` + `Authorization` from the web bundle | RLS hides rows on GET — the insert receipt is the only confirmation; `23505` → `ALREADY` |
| Supabase RPC | `POST .../rest/v1/rpc/<fn>` | same: receipt, not read-side |
| Next.js server action | multipart POST to the same page with an action id | the id lives in the bundle as `createServerReference)("…")` |
| Netlify Forms | `POST /` form-urlencoded with `form-name` | fields = the form inputs' `name=`; a real captcha goes through `bot/captcha.py` |
| Firestore REST | `POST .../documents:commit` with `writes[].update` + `updateTransforms` | `documents/create` rejects `serverValue` in a field (400); rules are usually create-only |
| Google Apps Script | `POST .../exec` JSON as `text/plain`, `allow_redirects=False` | a `302` to `script.googleusercontent.com` means the row landed; the echo page's code is **not** a verdict |
| Google Form | `POST formResponse` with `entry.<id>` | check `FB_PUBLIC_LOAD_DATA_` first: verified-email collection (`[1][10][1]==1`, `[18]==1`) means every POST answers 401 — refusal class, no adapter |
| Multi-step quest flow | one `requests.Session` per submit: create entry → verify tasks → submit wallet | the site can change routes mid-day; read the bundle, don't guess from error text |

## 4. Updating / removing

- **URL / body / success JSON changed** → edit `projects/<id>/api.py`.
- **User input format changed** → `module.py` (`parse`/`validate`/`help_html`).
- **Limit / name / order / links changed** → `project.toml`.
- **Disable:** `enabled = false` (or rename the folder with a `_` prefix).
  Prefer disabling over deleting — submissions history and dedup rows keep
  referencing the id. Full removal: delete the folder; DB rows stay.

## 5. Captcha: why an empty token is forbidden

**Rule: if the page has a captcha, solve it for real.** Even when the
endpoint happily accepts `turnstile: ""` and answers `201 {"ok":true}`.

Acceptance and inclusion in the final list are two different gates, and the
second one is invisible from the submit side. This was measured once: 5 979
rows filed with an empty token, every one answered `201 {"ok":true}` —
**zero** of those wallets made it into the final merkle tree, while 42 of 43
real submitters did. The server stored the rows and then filtered on exactly
`turnstile = ''`, which removes 100% of them, not "almost all".

The same applies to any client-filled "decorative" field — fabricated repost
links, `*_verified` flags, honeypots. A site that resolves the link or its
author is a refusal; auto-filling syntactically valid placeholders is only
acceptable where per-route measurement proved the endpoint does URL-shape
validation only, and that decision is written down per project.

Challenges are handled exclusively via paid solving services
(`bot/captcha.py`: Capsolver, 2Captcha, Anti-Captcha), each solve from a
configured balance or the user's own key (`/keys`). A project whose every row
costs a paid solve is VIP-only unless the user brought their own key.

## 6. Definition of done

- [ ] `projects/<id>/` with `project.toml` + `module.py` (+ `api.py`)
- [ ] `id` == folder name, `enabled = true`
- [ ] Registry sees the project; button → help → URL buttons → back works
- [ ] parse / validate / limits live in the module, not in the core
- [ ] No project id or links hardcoded in `tgbot.py` / `bot/*`
- [ ] Live smoke run on your own accounts and verified on the site's side
- [ ] Docs updated if the contract changed
