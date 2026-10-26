# Autoreg — Telegram whitelist-registration bot

A self-hosted Telegram bot that files **your own** wallet/handle accounts into
NFT allowlist ("whitelist") forms, one Python module per project, with a job
queue, tiered per-user limits, paid plans, proxy handling and a Telegram
Mini App on the same gates.

> **Consent-based by design.** The bot only ever submits accounts the user
> explicitly uploaded via `/data` (or typed/pasted themselves). It never asks
> for private keys — sites that require a wallet signature are out of scope.
> Captchas are solved only through paid third-party solving services
> (Capsolver / 2Captcha / Anti-Captcha), never bypassed.

## Features

- **One module per project** — `projects/<id>/` (`project.toml` + `module.py`
  + optional `api.py`). Copy `projects/_template/` to add your own.
- **Channel subscription gate** — the bot only talks to members of a channel
  you configure (the bot must be its admin).
- **Tiered per-project limits** — free (default 10, runtime-configurable),
  subscriber (via a file and/or a Telegram group), paid Basic plan, VIP
  overrides. Only successful (`OK`) submissions count against the limit.
- **Global wallet dedup** — a wallet already registered `OK`/`ALREADY` for a
  project is never submitted again by anyone.
- **Submit queue** — global FIFO, crash-safe (SQLite lease protocol), one
  editable progress message per job, VIP row parallelism.
- **Proxies** — shared pool (`proxy.txt` + extra files, URL-deduped) and
  per-user uploads (`/proxy`, all-or-nothing validation).
- **RU/EN i18n**, admin panel (`/admin`), broadcast outbox with 429/403
  handling, new-project announcements, closed-form auto-disable.
- **Payments (optional)** — NOWPayments USDT BEP-20 for the Basic plan;
  disable entirely with `PAYMENTS_ENABLED=0`.
- **Mini App (`webapp/`)** — projects, queue, mint calendar, settings; runs
  as a thread inside the bot process, layered authorization, rate limits.
- **Mint calendar** — quote-gated X (Twitter) fact extraction into a calendar
  with T-1h opt-in reminders. Off by default (`MINTS_ENABLED=0`).

## Requirements

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (optional) A channel the bot administers, for the subscription gate
- (optional) HTTP/SOCKS5 proxies, captcha solver keys, a NOWPayments key

## Setup

```bash
git clone <this repo> && cd autoreg
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

cp .env.example .env && chmod 600 .env
$EDITOR .env          # BOT_TOKEN at minimum; ADMIN_IDS for /admin

python tgbot.py
```

Read the full variable list and defaults in `bot/config.py`; the ones you
most likely want to set are annotated in `.env.example`.

For production use systemd (see `ops/tgbot.service` and `ops/README.md`):

```bash
sudo cp ops/tgbot.service /etc/systemd/system/
sudo systemctl enable --now tgbot
```

**Run exactly one poller per `BOT_TOKEN`.** Two processes polling the same
token get `409 Conflict` from Telegram and the bot looks dead.

### Persistent state

Wallet profiles, per-user proxies, queue payloads, the SQLite DB and the
results log live outside the checkout — by default `~/.autoreg`
(`AUTOREG_STATE_DIR` overrides). Never point them back into the repo or a
`git clean` wipes your users' data. Back the whole directory up:
`ops/backup_state.sh`; migrate between hosts: `ops/migrate_state.py`.

## Adding a project

Each whitelist site is a folder:

```
projects/<id>/
  project.toml   # id, name, enabled, order, ok_limit, links
  module.py      # parse/validate/submit adapter
  api.py         # (optional) HTTP for the site
```

```bash
# Recon the site's API, then scaffold a draft folder:
python tools/scaffold_project.py https://example.com --id myproj --init
# ... finish the adapter by hand (docs/contract: PROJECTS.md) ...
# Pre-flight on your own test accounts, then verify on the site's side:
python tools/smoke_project.py myproj --user <your_telegram_id> --count 10 --dry-run
python tools/smoke_project.py myproj --user <your_telegram_id> --count 10
```

Then set `enabled = true` in `project.toml` and restart (or `/reload` as
admin). The module contract, `project.toml` fields, API cookbook and the
definition of done: **[PROJECTS.md](PROJECTS.md)**.

**Submission integrity rule:** never file an attestation the endpoint could
verify as false — no empty captcha tokens, no links to posts that do not
exist. Acceptance is not inclusion: a site can accept a row and silently drop
it from the final list. See PROJECTS.md §"Captcha".

## User flow

```
/start → channel gate → project list (health icons, per-project progress)
  → pick project → project screen (slots, proxies, requirements)
  → [▶ Start] → one queue job over the /data profile
/queue /cancel /stats /lang /help /limit /check /priv
```

Users upload accounts with `/data` (`handle|0xWallet` per line) and optional
own proxies with `/proxy`. Admins get `/admin` (stats, user cards, errors,
project health/enable, broadcasts, payments, `/reload`).

## Layout

| Path | Role |
|------|------|
| `tgbot.py` | wiring + submit orchestration; handlers in `handlers/` |
| `bot/` | core: config, SQLite (`bot/db/`), registry, limits, queue, proxies, i18n, captcha, billing, broadcast, alerts |
| `projects/` | one folder per project; `_template/` is the starter |
| `webapp/` | Telegram Mini App (thread inside the bot process) |
| `tools/` | scaffolding and live smoke-test helpers |
| `ops/` | systemd units, backup/migration scripts, runbook |
| `tests/` | pytest suite (`pytest -q tests/`) |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q tests/
```

## Legal / acceptable use

This tool automates form submissions **for accounts their owner uploaded**.
Rate limits, dedup and per-user caps are built in — respect the target
sites' terms of service and local law. The authors take no responsibility
for abuse.
