# AGENTS.md — architecture guide for coding agents

Instructions for humans and coding agents working in this repo. Language:
code, comments and commit messages in English. User-facing Telegram strings:
RU/EN via `bot/i18n.py`.

## What this is

A Telegram bot that files whitelist/allowlist registrations for accounts the
user explicitly uploaded (`/data`: `handle|wallet` lines) — one Python module
per project (`projects/<id>/`), a global submit queue, tiered per-user
limits, optional paid plans, plus a Telegram Mini App on the same gates.
Start with `README.md` for setup; the module contract is `PROJECTS.md`;
hard-won bug stories are in `docs/PITFALLS.md`.

## Non-negotiable rules

1. **Submission integrity** — never submit an attestation the endpoint could
   verify as false: an empty captcha token, a link to a post that does not
   exist. Acceptance is not inclusion; a site can accept a row and drop it
   from the final list. See PROJECTS.md §5.
2. **Live tests only on accounts you own**, small counts, and you verify the
   rows landed on the project's side — not just in this bot's database.
3. **Never commit `.env`, `proxy.txt`, `*.db`**, `bot_results.jsonl` or any
   backup of them.
4. **Never run a second poller** with the same `BOT_TOKEN` (Telegram 409).
   Restart the bot when a change needs it.
5. **Treat 409 / unique-violation as `ALREADY`**, never as `FAIL`.
6. **No secrets in `project.toml`** — use `.env`.

## Layout

| Path | Role |
|------|------|
| `tgbot.py` | create_bot wiring + submit orchestration; handlers in `handlers/` |
| `bot/config.py` | every env var with defaults and comments (authoritative list) |
| `bot/db/` | SQLite layer; DDL + numbered migrations in `bot/db/schema.py` |
| `bot/registry.py` | project discovery (`projects/` + `$STATE_DIR/user_projects/`) |
| `bot/limits.py` | tier ladder: free / subscriber(file+group) / paid / VIP; `tier_caps` |
| `bot/submit_queue.py` | FIFO job queue, progress messages, lease protocol |
| `bot/screen.py`, `bot/ui_screens.py` | one anchored, editable message per user |
| `bot/http_client.py` | shared HTTP profile, proxies, wallet check, GAS-lock helpers |
| `bot/captcha.py` | Turnstile via paid solving services only |
| `bot/gform.py` | user Google-Forms builder (runtime adapters) |
| `bot/broadcast.py` | durable paced outbox — never loop `send_message` |
| `bot/billing.py`, `bot/payments_*.py` | NOWPayments plan catalog + poller |
| `bot/privatka.py` | optional external-upsell screen (`UPSELL_BOT`, empty = hidden) |
| `projects/<id>/` | one folder = one project; `PROJECTS.md` is the contract |
| `webapp/` | Mini App thread inside the bot process (`webapp/README.md`) |
| `tools/` | `scaffold_project.py`, `new_project.py`, `smoke_project.py` |
| `ops/` | systemd unit, backup/migration, runbook |

## Key behaviors to keep intact

- **Tier ladder** (`bot/limits.py: effective_ok_limit`): DB VIP override →
  hardcode/env VIP map → max(active paid plan, subscriber sources that **add
  up**, free limit). Gaining a tier can never lower a user. Own proxies
  change only the traffic source, never the limit.
- **Global wallet dedup** — `OK`/`ALREADY` for `project_id+wallet` from any
  user blocks a resubmit before HTTP.
- **Captcha projects are VIP-only** unless the user set their own solver key
  (`/keys`): nobody else's balance is spent on a non-VIP row
  (`bot/limits.py: project_allowed`).
- **Closed forms self-disable** (`bot/form_closed.py`, `auto_close_on_gate`):
  runtime flag in SQLite, soft-cancels the project's jobs, reversible from
  the admin panel; `/reload` does not clear it.
- **One anchored screen per user**; the queue dashboard is a separate message
  (`submit_jobs.message_id`), not the anchor.
- **Mini app writes go through `tgbot.BotActions`** → the same
  `enqueue_if_allowed` gates as the chat; `webapp/server.py` never imports
  `tgbot`. The queue lives in the bot process's memory — a job inserted from
  another process is a row no worker will ever pop.
- **Persistent runtime state** lives outside the checkout (`~/.autoreg`,
  `AUTOREG_STATE_DIR`): user data, proxies, queue payloads, DB, results.
  Never move the defaults back into the repo.
- **Migrations** are appended numbered steps in `bot/db/schema.py`
  (`MIGRATIONS`), idempotent, stamped via `db_schema_version`; payment
  amounts are TEXT decimal strings, never REAL.

## Conventions

- Stdlib + `requests` + `pyTelegramBotAPI` + `python-dotenv`; threading model
  is per-user lock + global submit semaphore.
- Small focused diffs; no drive-by refactors; no unsolicited markdown files.
  Update `AGENTS.md` / `PROJECTS.md` when product behavior changes.
- User-visible errors: one human sentence (`bot/errors.py`); raw bodies and
  HTTP codes stay in SQLite/logs/admin panel.
- Tests: `pytest -q tests/` (CI: `.github/workflows/test.yml`). Run them
  before finishing any change to `bot/` or `handlers/`.
