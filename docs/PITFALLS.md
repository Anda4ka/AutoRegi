# PITFALLS.md — agent pitfalls (do not reintroduce)

Moved out of AGENTS.md 2026-09-14: hard-won bugs with the reasoning that keeps
them from coming back. Read before editing submitters, UX, registry, docs;
the six non-negotiable rules (including the captcha/proof rule) live in
AGENTS.md «Read this first».

Hard-won bugs. When editing submitters / UX / registry, re-read this.

### 16.0 Never send an empty captcha token

**If the page renders a captcha, solve it — always** (`bot/captcha.py`), even
when the endpoint happily accepts a body without one. "The server does not
verify it" means "the server does not verify it *at submit time*, and it is
storing what we send".

Measured on a project since removed from the bot, 2026-08-14. Its route took `turnstile: ""` and
answered `201 {"ok":true}`; the adapter sent an empty string on purpose and said
so in a comment. The bot logged **5979 accepted registrations**. When the merkle
tree went on-chain, **0 of the 14 101 wallets the bot had ever handled were in
it** — while 42 of 43 real minters were, and none of those had ever passed
through the bot. Acceptance and inclusion are two separate gates; the second one
is invisible, and filtering on one stored column removes exactly 100% of our
rows. A clean zero is the signature of an exact-match filter — any behavioural
heuristic (velocity, IP, pattern) would have left survivors.

Another project ran the same empty token and started enforcing at the door
instead (`403 "Verification failed"`), which is the only reason that one was
caught. It was removed on 2026-08-15; the rule outlived it.

Rules:

- Solve per attempt — a Turnstile token is **single-use**, Cloudflare burns it on
  the first verify, so a retry solves a fresh one instead of resending.
- **Copy the widget's `action` into the solve.** `turnstile.render({sitekey,
  action})` is not cosmetic: Cloudflare echoes the action back from siteverify
  and a server that reads it will refuse a token minted without one. The refusal
  is `verification_failed` — the same words as a wrong key, a burnt token or an
  IP mismatch — so it is invisible unless you read the render call. `etherbubu`
  cost a whole 10-wallet run to learn it (2026-08-21: ~35 valid tokens, every
  one refused, nothing written). Before buying a second solve for a token you
  believe is good, grep the bundle for `sitekey` and take everything next to
  it — `action`, and `cdata` where a site uses one.
- No key / no balance → `FAIL` with a captcha reason. Never fall back to an
  empty token.
- **Two solver accounts, and the second is not decoration.** An account that
  runs dry mid-run fails in a way that reads exactly like the site refusing the
  row (2026-08-21: a $0.0004 Capsolver balance stopped an `etherbubu` run
  before one request reached the form). `solve_turnstile` walks the configured
  providers and the first token wins. Two invariants hold the seam together: a
  provider that answered permanently is **remembered** so later rows do not pay
  a round-trip to hear it again, and a dead account's «no balance» is **never**
  the detail returned while another account gave a transient error — otherwise
  `is_captcha_solver_permanent_error` would stop a row the live account could
  still have filed.
- The same logic covers every field the page computes client-side (`*_verified`
  flags, attempt counters, honeypots): send what a real visitor's browser would.
- Tests must never reach the solver — `tests/conftest.py` blanks
  `CAPSOLVER_API_KEY` **and** `TWOCAPTCHA_API_KEY`; a test that needs a token
  stubs `solve_turnstile`. `tests/test_captcha.py` pins
  `CAPTCHA_PROVIDER_ORDER` so its assertions describe the code and not the
  operator's current `.env`.

### 16.1 Status mapping — never forge BUSY

| Status | Meaning | Count toward `ok_limit`? |
|--------|---------|--------------------------|
| `OK` | new accept | yes |
| `ALREADY` | already registered | no |
| `BAD_RESP` | permanent business / 4xx reject | no |
| `FAIL` | network / proxy / retries exhausted | no |
| `BUSY` | **only** real sheet/LockService contention (GAS modules) | no |

**Wrong (do not revive):**

```python
# bot/ux.py — NEVER do this
is_retryable_failure(detail, reason="gas_busy")  # reason short-circuits → True for almost any detail
```

That path turned invalid twitter / HTTP 400 / empty detail into “Google Sheet lock”, faked DB rows as `BUSY`, and made `FAIL` unreachable in `submit.py` final status.

**Right:**

- `is_busy_detail` / longer lock backoff / final `BUSY` → only `is_gas_lock_message(detail)` (or equivalent lock keywords in the **detail**).
- Pass a real `reason` from the response only when you actually classified it (`duplicate`, `gas_busy` from lock text, etc.). Do not invent `reason="gas_busy"` just to reuse a helper.
- Permanent rejects (`invalid twitter`, `invalid wallet`, 409 unique) → `BAD_RESP` or `ALREADY`, **stop retries**, never `FAIL`/`BUSY`.

### 16.1.1 The Apps Script echo page is not the project's answer

`blastoons`, `cultnft` and `frankiez` all end at the same place: a
successful `doPost` answers `302` to `script.googleusercontent.com/macros/echo`.
**That redirect is issued after `doPost` returns, so it alone proves the row was
written.** The echo page is a second hop that carries only the *verdict*, and
through a rotating proxy pool it is unreliable:

- Roughly **one exit IP in five** gets `HTTP 404` there (measured 2/10,
  2026-08-15) while the row it belongs to is already in the sheet.
- Some exits get `HTTP 200` carrying Google's user content **sandbox wrapper**
  (`window['ppConfig']`, `productName: '26981…'`) instead of the raw body,
  localized by exit IP — `lang="en"`, `"id"`, `"ru"`, `"th"` all seen.

So the second hop's HTTP code must never become the row's status, and HTML there
is **not** a thrown `doPost` — there is no way to read a script failure out of
that page. The live bot learned this the hard way on 2026-08-15: two adapters
reported the echo's `404` as `BAD_RESP`, which matched `bot/form_closed.py` and
raised a false «registration closed» alert on rows that had landed — one of them
was runtime-disabled by the resulting admin button.

Rules:

1. `allow_redirects=False`, always. Letting `requests` follow merges the hops and
   lets the echo speak for `/exec`.
2. A `302` to the echo host = **OK**.
3. Fetch the echo **only** when its body carries something `/exec` does not
   imply — `frankiez` needs it for `duplicate`; `cultnft` does not, because
   its script appends unconditionally and answers success to any input.
4. When you do fetch it, only **parsed JSON** is a verdict. A 404, wrapper HTML
   or a dead proxy leaves the row OK with the reason in the detail. The cost —
   a hidden `duplicate` counted as a row — is far cheaper than losing a stored
   row and disabling the project.
5. Only `404`/`410` from `/exec` itself means the deployment is gone.

Related: `is_html_body(raw: str)` takes a **body**, not a `Response`. Handing it
the response object raises inside the `try`, and the adapter reports the
`AttributeError` as a proxy failure after a full round of retries — the script's
actual error never reaches the log. Fixed in `blastoons` on 2026-08-15 with a
regression test in `tests/test_new_projects.py`.

### 16.2 Proxy URL rebuild after DNS pin

`load_proxies` already percent-encodes user/pass (`quote(..., safe="")`).

When replacing hostname with IP:

- **Keep** original userinfo from `urlparse(...).netloc` (substring before `@`) **as-is**.
- **Never** `quote(parsed.username)` / `quote(parsed.password)` again — `urlparse` may leave `%3A` literal; second `quote` → `%253A` → HTTP 407.
- IPv6 literals must be bracketed: `http://user:pass@[2001:db8::1]:8080`.

Password may contain `:` (proxy line split is `maxsplit=3`).

### 16.2.1 Fingerprint must not contradict itself

`random_fingerprint(origin, referer, url=...)` — **pass `url` whenever the
request goes to the same origin as the page**. Chrome/Edge fingerprints send
`sec-fetch-site: cross-site` by default; combined with the site's own `Origin`
that is a contradiction no real browser produces, and ZTH Academy rejected
~60% of submissions with `ORIGIN_NOT_ALLOWED` because of it — only the
Safari/Firefox fingerprints (which send no `sec-fetch-*`) were getting through.

`url=` is safe to pass everywhere: when the target really is another host
(GAS, Google Forms, an `api.` subdomain) the value stays `cross-site`.

Bunny Hood named it outright on 2026-08-18: the first row its route ever saw
came back `400 {"error":"Cross-site submissions are not accepted."}`, and the
only change that fixed it was passing `url=URL`. A route that says this is not
rejecting the wallet — it is reading two headers that disagree.

### 16.3 EVM address extract from free text

```python
# BAD — matches first 40 hex of a 64-char tx hash
r"0x[a-fA-F0-9]{40}"

# GOOD — refuse longer hex tails
r"0x[a-fA-F0-9]{40}(?![0-9a-fA-F])"
```

Otherwise a pasted tx hash becomes a garbage “wallet”, gets `OK`, burns an ok-slot.

### 16.4 Direct HTTP without hardcoding project ids

- **All projects use proxies** — `resolve_proxies` never returns `direct` (`prefer_direct` / VIP `allow_direct` ignored).
- Resolve path: own (valid) → shared → none. Corrupt own file → none (no silent shared fallback).
- **Forbidden:** `if project.id not in ("demo-project",):` (or any hard-coded ids) in `tgbot.py`.
- Startup: empty shared `proxy.txt` is a **warning** (own proxies may still work); without any proxies submit fails with `no_proxies`.

### 16.5 Registry numeric defaults

Do not use `or` for integers that may be `0`:

```python
# BAD
ok_limit = int(raw.get("ok_limit") or OK_LIMIT)  # 0 becomes default

# GOOD
if raw.get("ok_limit") is not None and raw.get("ok_limit") != "":
    ok_limit = int(raw["ok_limit"])
```

### 16.6 Parse / help consistency

- Strip leading `@` on twitter handles in bot `parse` (same as `normalize_twitter` in api/http_client).
- `help_html` examples: **placeholders only** (`myHandle`, `0x1234…abcd`), not real operator accounts.
- DNS sample loops: `[: min(sample, len(hosts))]`, not `max`.

### 16.7 Limits: always use `effective_ok_limit` (tiered)

```python
# BAD — ignores the tier ladder and hardcodes toml
if ok_count >= project.ok_limit: ...

# GOOD
lim = bot_limits.effective_ok_limit(user_id)  # or config.effective_ok_limit(user_id)
if ok_count >= lim: ...
```

Same for `/limit`, project screen, bulk cap, `format_result(..., ok_limit=lim)`.

### 16.8 Single Telegram poller

- **Never** run two `python tgbot.py` with the same `BOT_TOKEN` (screen **and** systemd).  
- Symptom: `409 Conflict: terminated by other getUpdates request`.  
- Fix: one process; see `ops/README.md`.  
- Do not call `getUpdates` manually against a live bot token.

### 16.9 Bulk / proxy documents without hardcoding projects

- Data bulk: all non-banned users with project selected + `bulk_columns`.  
- Proxy file: only `_awaiting_proxy` (from `/proxy` or admin proxy menu).  
- Schema: `project.bulk_columns` from `get_project()`.  
- Never bypass `record_submission` / ok counters.

### 16.10 Own proxies: valid-only has_own

```python
# BAD — any non-empty line ⇒ "has own" then resolve falls through to shared
if file has text: return OWN_LIMIT and maybe shared

# GOOD
has_own_proxies := bool(load_user_proxy_urls(...))  # parse must succeed
# corrupt non-empty file → resolve none (not shared)
```

### 16.11 Admin fail-closed + ban gates

- Empty `ADMIN_IDS` ⇒ deny admin (unless `ALLOW_OPEN_ADMIN=1`).  
- Ban check on: text submit, document, all `proxy:*`, suggest, **project:**, help/limit/check, back/subcheck.  
- `/start` for banned: ban message only (no project keyboard).

### 16.12 Limits: subscriber tier + own caps

```text
VIP/DB → hardcode VIP → max(paid plan, subscriber tier) → free
```

- Do **not** treat `active_subscribers.txt` as VIP (bulk stays unpaid caps unless true VIP).
- Own proxies grant no OK limit (2026-09-09); their only cap is `USER_PROXY_MAX_LINES` (1000 lines) on the file itself.
- After editing subscriber, shared proxy files, or project.toml: admin `/reload` (or restart). Env concurrency/threads / `.env` secrets need **process restart**.

### 16.13 Concurrency vs “bot mute”

- `BOT_HANDLER_THREADS` = TeleBot worker pool (handlers).
- `BOT_CONCURRENCY` = queue worker count **and** global `_submit_pool` size (max simultaneous `project.submit`).
- A job spawns `bot_limits.job_parallel(user_id)` row threads (unpaid `BASE_JOB_PARALLEL`, Basic one above it, Pro/VIP `VIP_JOB_PARALLEL`; the ladder is asserted in `tests/test_billing.py` — raising the unpaid tier must never reach Basic); they still compete for `_submit_pool`. The per-user `_user_lock` in `queue_submit_one` is taken only for single-threaded tiers — parallel tiers rely on `wallet_claims` instead, or the lock would serialize them right back.
- Long rate-limit sleeps inside submit hold a pool slot. Prod often uses **10/10**.

### 16.14 Queue, `/data`, global dedup

- One job = one project’s rows from `/data` (or prepared fields); max **3** active jobs/user; **one running job per user** (chain is sequential).
- Process restart **no longer kills jobs**: the lease expires within `QUEUE_LEASE_SECONDS` and the sweep resumes the job from its unfinished rows. Mark `error` only for a real terminal failure.
- Global dedup: `project_id + lower(wallet)` with status OK/ALREADY (any `user_id`). FAIL does not block. Do not invent cross-project dedup.
- Synthetic status URLs are for bot autofill only — not real RTs; keep the snowflake window at **12h** (`MAX_AGE_HOURS`, one place for every project) and handle max **15**. A link older than the registration it proves is what a manual review notices first, and the floor from `after_id` never widens the window — it only raises it.
- Never log proxy passwords or dump full `/data` files.

### 16.15 Money and the anchored screen

- **Never grant a plan outside `db.credit_payment_if_new`.** It is the only
  place that flips `credited_at`, and that flip is what stops a repeated
  `finished` status, a poller restart or two threads from crediting twice.
- **Never put a NOWPayments body in user text.** `NowPaymentsError.detail` and
  raw submit details go to the log, `submissions`/`payments` and the admin
  panel; users get `bot/errors.py`.
- **Submit gates read `effective_ok_limit(..., fresh=True)`** — the tier cache
  is for rendering. `try_reserve_ok_slot` remains the atomic authority.
- **Two threads must not edit the anchor without `screen.user_lock(uid)`.**
- **Do not route queue progress through the anchor.** It was tried: the job
  claimed the user's current screen at start (so `/start` turned into a
  progress bar) and an `owns_anchor` guard then silently dropped every redraw
  once the user navigated away, freezing the counter. Dashboard and navigation
  are separate messages on purpose.
- **A paid plan is not `is_vip()`.** Use `tier_caps` / `job_parallel` /
  `may_upload_proxies` for capability; `is_vip` stays "admin granted".

### 16.16 Never loop `send_message` over an audience

Everything that goes to more than one user goes through `bot/broadcast.py`.
The loop it replaced looked harmless — `send_message`, `sleep(0.05)`,
`except Exception: pass` — and was wrong four ways at once:

- a `429` was swallowed, so the message was **lost** while the counter still
  reported it as delivered;
- a `403` (user blocked the bot) was indistinguishable from a network blip, so
  the same dead chats were retried on every future broadcast;
- it ran inside the callback thread, freezing that admin's UI for the whole
  send;
- a restart mid-send lost the remainder with no record of where it stopped.

Rules:

- Enqueue and let the sender thread deliver: `broadcast.enqueue(batch_id, ids,
  …)`. Report what was **queued**, and read delivery from
  `broadcast_batch_stats`.
- A batch id must be stable and unique — `(batch_id, user_id)` is what makes a
  re-enqueue idempotent. Never build one from a value that changes between
  retries.
- Pace with `interval_sec` when the *destination* needs it (a project's intake);
  the bot-wide floor already covers Telegram.
- Announcements that fire without a human must be marker-deduped **and**
  admin-approved. An alert loop with neither will eventually blast the base
  twice.

### 16.17 Never reassemble a doc by line slicing

The obituary move (2026-09-08) was scripted as "keep lines 1–142, append the
pointer" — and that is all it kept: every section after the removed block
(the module contract, the whole add-a-project checklist, the channel gate,
the cookbook) silently vanished and shipped in a -1402-line diff that nobody
read, because the commit also carried a thousand lines of new code. The file
looked healthy at the top and the pipeline section still worked.

Rules:

- Moving a section out of a document means **replacing that section**, never
  "head + new text". Write `head + tail + new` where `tail` is spelled out,
  or use the edit tool on the section boundaries.
- After any scripted rewrite of a doc, `wc -l` against the previous version
  and account for every line of the delta before committing. A docs commit
  that deletes a thousand lines is never just housekeeping.
