# Ops: running the Telegram bot

## Single poller rule

Telegram allows **one** `getUpdates` consumer per bot token.

- Use **either** `screen -S tgbot` **or** systemd `tgbot.service` — **never both**.
- Symptom of two processes: `409 Conflict: terminated by other getUpdates request`.

## Architecture (bot-only)

| Path | Role |
|------|------|
| `tgbot.py` | Telegram handlers, polling |
| `bot/` | config, DB, registry, limits, subscribers, proxies, queue, data, i18n, http_client |
| `bot/http_client.py` | shared fingerprint / load_proxies / GAS helpers |
| `bot/limits.py` | free 10 / subscriber ≥50 / Basic 100 / VIP 1000 (own proxies grant no limit) |
| `bot/subscribers.py` | `active_subscribers.txt` |
| `bot/group_tier.py` | `SUBSCRIBER_GROUP` membership → same tier (getChatMember, bot must be group admin) |
| `bot/user_data.py` | `/data` handle\|wallet profile |
| `bot/submit_queue.py` | job queue, progress, VIP×5 rows, boot reconcile |
| `bot/tweet_urls.py` | synthetic X status URLs for queue autofill |
| `bot/broadcast.py` | outbound queue: pacing, tier order, 429/403 handling |
| `bot/new_project_alert.py` | new registry id → admin preview → announcement |
| `bot/i18n.py` | RU/EN user strings (`users.lang`) |
| `projects/<id>/module.py` | parse / validate / help |
| `projects/<id>/api.py` | project HTTP submit |
| `proxy.txt` | shared proxies (chmod 600) |
| `message.txt` | extra shared proxies (`EXTRA_PROXY_FILES`) |
| `active_subscribers.txt` | TG ids with shared OK limit 50 |
| `SUBSCRIBER_GROUP` (.env) | supergroup whose members get that same 50, live |
| `~/.autoreg/user_data/` | persistent per-user `/data` files |
| `~/.autoreg/job_payloads/` | persistent queue payloads |
| `~/.autoreg/tgbot.db` | persistent users, stats, submissions, `submit_jobs`, `broadcast_outbox` |

**No** top-level `submit_*.py` CLI. After code changes: **restart the poller** (not only `/reload_projects` if `api.py` / core changed).


## Persistent state (wallet-loss prevention)

Runtime state is deliberately stored outside the Git checkout. Default is
`~/.autoreg`; the production unit sets `AUTOREG_STATE_DIR=/var/lib/autoreg`.

The state directory contains `tgbot.db`, `user_data/`, `user_proxies/`,
`job_payloads/`, `proxy.txt`, `active_subscribers.txt`, and
`bot_results.jsonl`. Replacing the Git checkout (`/opt/autoreg`) must
never replace this directory.

On the first start after upgrading, the bot copies a legacy in-repo state into
the persistent directory when the destination is missing. It never overwrites
existing destination files. Startup logs the selected paths as `[state] ...`.

To recover wallets from an older checkout, stop the bot, locate the old files,
dry-run the migration, then copy:

```bash
sudo systemctl stop tgbot
find /var/lib/autoreg -path '*/data/user_data/*.txt' -type f -printf '%h\n' | sort -u

cd /opt/autoreg
python ops/migrate_state.py --from-root /opt/autoreg-old --to /var/lib/autoreg --dry-run
python ops/migrate_state.py --from-root /opt/autoreg-old --to /var/lib/autoreg
chmod -R go-rwx /var/lib/autoreg
sudo systemctl start tgbot
```

Override `AUTOREG_STATE_DIR` only when the same explicit path is configured for every runner.


`/reload` (admin) reloads **shared proxies + subscribers + projects** without process restart.  
Aliases: `/reload_proxies`, `/reload_projects` (same full reload).

## systemd (current default)

Production is the `tgbot` unit (`ops/tgbot.service`): checkout
`/opt/autoreg`, state `/var/lib/autoreg`.

```bash
sudo systemctl restart tgbot
sudo systemctl status tgbot
journalctl -u tgbot -n 80 --no-pager
```

Do not also run `screen -S tgbot` or a second `python tgbot.py` with the same token.

Healthy startup should show:

```text
[registry] loaded … (projects)
[subscribers] loaded N id(s) from …/active_subscribers.txt
[proxies] merged proxy.txt:… + message.txt:… → unique=…
Concurrency: submit=10 handler_threads=10
Limits: free=10 subscriber=50 basic=100 vip=1000 …
[queue] boot reconcile: interrupted_running=N legacy_payload=M   # lease jobs resume themselves
[queue] started 10 workers
Bot starting …
Polling: long_poll=…
```

No `409 Conflict` lines.

## Limits / reload cheatsheet

| Action | How |
|--------|-----|
| Edit `proxy.txt` / `message.txt` | `/reload` or restart |
| Edit `active_subscribers.txt` | `/reload` or restart |
| Change `BOT_CONCURRENCY` / threads | **restart** (env at process start) |
| Enable/disable project | `project.toml` + `/reload` or restart |

## Routes that verify the repost author

Some day a route appears that resolves the repost link and checks its author.
Two rules worth keeping for any route of that shape (see PROJECTS.md):

* **an id is held, not spent, until the route has answered.** Spending it at
  the draw burns ids on every local refusal, retry and cancelled row — rows
  the route never even saw;
* **ask whether the site checks the author before collecting anything.** A
  stock of other people's posts becomes worthless the afternoon the site
  starts checking.

## screen (fallback)

Use only when systemd is not the runner. Stop the unit first.

```bash
sudo systemctl stop tgbot
cd /opt/autoreg
source venv/bin/activate
screen -S tgbot -X quit
screen -dmS tgbot bash -lc 'cd /opt/autoreg && . venv/bin/activate && exec python -u tgbot.py >>/tmp/tgbot_run.log 2>&1'
screen -ls
# attach: screen -r tgbot
```

Logs in this mode: `/tmp/tgbot_run.log`.

## logrotate (example)

```text
/tmp/tgbot_run.log {
    weekly
    rotate 4
    missingok
    notifempty
    compress
    copytruncate
}
```

## Announcements and broadcasts

Everything sent to many users at once goes through the SQLite outbox
`broadcast_outbox` (`bot/broadcast.py`), never a loop over `send_message`. One
row per recipient, one background sender thread, started with the bot.

**Reading the state of a send:**

```bash
sqlite3 ~/.autoreg/tgbot.db \
  "select batch_id, state, count(*) from broadcast_outbox group by 1,2;"
```

`pending` = still queued · `sent` = delivered · `blocked` = the user blocked the
bot (also flagged in `users.blocked_bot`) · `failed` = gave up after
`BROADCAST_MAX_ATTEMPTS`. In Telegram the same numbers are behind
«🔄 Прогресс» on the batch message; «🛑 Остановить» drops only what has not
gone out yet.

- A restart **resumes** a half-sent batch — rows are in the DB and
  `(batch_id, user_id)` is unique, so nothing is sent twice. The startup log
  prints `[broadcast] resuming N batch(es)`.
- Telegram `429` is honoured: the row keeps its place with Telegram's own
  `retry_after` and the whole sender pauses. Nothing is silently lost.
- Finished rows are purged after 14 days at startup. `ops/prune_db.py` is
  project-scoped and does **not** cover this table.

### New-project alert

Adding a project (a new id in the registry after a reload) sends `ADMIN_IDS` a
preview of the announcement — both languages, recipient count, projected
runtime — with «📣 Разослать» / «✖️ Не слать». **Nothing reaches users without
that tap.**

- Audience: users who submitted something in the last 30 days (not the channel,
  not everyone who ever pressed /start).
- Pace: **1 recipient / 10s, best tier first** (vip → basic →
  subscriber → free). 130 recipients ≈ 22 minutes. The pace is about the
  project's own intake, not Telegram's limits.
- Offered **once per id**. Tapping «Не слать» retires it; so does a delivered
  preview nobody answers. To announce such a project later, use 📣 Рассылка by
  hand.
- On a fresh database the first start **seeds** `bot_settings.known_project_ids`
  with the current projects and announces nothing. Verify after an upgrade:

```bash
sqlite3 ~/.autoreg/tgbot.db "select value from bot_settings where key='known_project_ids';"
```

- Off switch: `NEW_PROJECT_ALERTS=0` (env, needs restart) or the
  «🆕 Алерт о новых проектах» toggle on `/admin` → 📁 Проекты. While off, new
  ids are absorbed silently, so switching back on cannot release a backlog.
- A project disabled on disk is outside the registry, so **re-enabling it later
  reads as new** and will be offered. That is intended — it is also the one case
  where an "old" project produces a preview.

Env (all optional, restart to apply): `NEW_PROJECT_ALERTS`,
`NEW_PROJECT_ALERT_INTERVAL`, `NEW_PROJECT_ALERT_ACTIVE_DAYS`,
`BROADCAST_MIN_INTERVAL`, `BROADCAST_MAX_ATTEMPTS`, `BROADCAST_RETRY_BASE`,
`BROADCAST_RETRY_CAP`, `BROADCAST_IDLE_SLEEP`.

## Admin panel

Telegram: **`/admin`** — Статистика · Пользователь · Лимиты · Ошибки · Проекты · **Здоровье** · Обновление · Рассылка
(only `ADMIN_IDS`; empty list = no admin access).

📁 Проекты also carries the new-project alert toggle; 📣 Рассылка now queues
instead of sending inline, so the confirmation reports what was **queued** and
the batch buttons report what was actually delivered.

`🩺 Здоровье` performs cached read-only project probes. Use «Проверить сейчас»
after changing `health_url` or captcha/other required env keys; no registration
payload is sent by this check.

Slash shortcuts: `/set_free_limit`, `/set_vip_limit`, `/del_vip_limit`, `/limits_config`, `/get_limit`, `/reload`.

**VIP is admin-only** (`/set_vip_limit`) — no monetization / no “buy VIP” UX.

## User commands

| Command | Role |
|---------|------|
| `/start` `/projects` | Project list (remaining slots, badges) |
| `/lang` | RU / EN (`users.lang`) |
| `/proxy` | Own proxies (→ own OK limit 50) |
| `/data` | Account profile; upload `handle\|0xWallet` or `handle,0xWallet` |
| `/queue` | Job status / chain; an idle queue can offer a new job when a project is selected |
| `/cancel` | Stop own running/queued job |
| `/stats` | OK summary all projects |
| `/limit` `/status` | Per-project counters |
| `/help` | Current project help |
| `/check` | Re-check channel subscription |

Inline (examples): `project:<id>`, `projects:back`, `lang:ru` / `lang:en`, `queue:status`, `queue:cancel:<id>`, `data:menu`.

## Limits (submit gate)

| Mode | Default OK/project | Proxies |
|------|-------------------|---------|
| free | 10 | shared `proxy.txt` (+ EXTRA) |
| subscriber | 50 (100 with both file and group) | shared (`active_subscribers.txt` / `SUBSCRIBER_GROUP`, grandfathered forever) |
| basic (paid $50/30d) | 100 | shared or own |
| VIP | 1000 | shared / own |

`project.toml` `ok_limit` is **not** the gate. Runtime free via admin or SQLite.
Own proxies grant **no** limit (2026-09-09) — they only change the traffic source.
A paid plan joins the `max()` of tiers, so it can only raise a user.

## Queue (ops notes)

- Workers = `BOT_CONCURRENCY`; change needs **process restart**.
- Rows per job run in parallel by tier (`bot_limits.job_parallel`: unpaid `BASE_JOB_PARALLEL`, Basic one above it, VIP `VIP_JOB_PARALLEL`); HTTP still limited by the global semaphore, and the per-user lock applies only to single-threaded tiers — above that, `wallet_claims` is what prevents duplicates.
- A user may have `bot_limits.parallel_jobs` jobs RUNNING at once (`USER_PARALLEL_JOBS`=2, Basic 2, VIP more) — raise it only if `GLOBAL_REQUESTS_PER_MINUTE` still has headroom. Measure before tuning: on 2026-08-13 the queue ran at 11 rows/min against a 300 ceiling, so workers and rpm were never the constraint — `job_parallel` was.
- Progress lives in the chain's own message, not the navigation screen; deleting it makes the next redraw post a fresh one.
- SQLite is the queue's source of truth (lease protocol): `_jobs` is a render cache.
- Global wallet dedup uses `submissions` history — do not wipe DB casually.
- Queue jobs ARE resumed after restart: an interrupted job's lease expires within
  `QUEUE_LEASE_SECONDS` (120 s) and the next claim requeues it, unfinished rows back
  to pending. Done rows are never re-taken; a row in flight at the crash may be
  submitted twice and answers ALREADY through the global wallet dedup.
- A VIP job can display `0/N` for a while on HoodHunters: each session token has a
  default 60-second gated delay (`HOODHUNTERS_MIN_STEP_DELAY`) before submit. This is
  intentional throttling, not an automatic success. Check service logs and `/queue`
  before cancelling.

## `/data` profile and validation

- The profile is stored outside SQLite at `USER_DATA_DIR/<tg_id>.txt` (normally
  `~/.autoreg/user_data/`) and is kept after a job finishes, is cancelled, or fails.
- The upload is all-or-nothing. If any non-empty line is invalid, the old profile is
  preserved and Telegram reports the first invalid line. If there was no previously
  valid profile, the UI shows `Accounts: not uploaded`; a valid old profile remains
  visible and usable.
- Accepted lines are exactly `handle|0xWallet` or `handle,0xWallet`. Handles must be
  1–15 ASCII letters/digits/underscore; wallets must be exactly `0x` plus 40 hex
  characters. The profile caps are 1000 lines and 512 KiB.

## Tests (dev)

```bash
cd /root/bot-regaauto-clean
source venv/bin/activate
pip install -r requirements-dev.txt
python -m tests.test_limits_proxies
python -m tests.test_dedup
python -m tests.test_audit_package_a
python -m tests.test_audit_package_b
python -m tests.test_broadcast_queue      # 429 / 403 / restart / drip
python -m tests.test_new_project_alert    # seeding, offer-once, localization
# optional:
# pytest -q

# everything, the way CI does it (one process per module, full isolation):
for m in $(ls tests/test_*.py | sed 's#tests/##; s#\.py$##'); do python -m tests.$m || echo "FAIL $m"; done
```

`requirements-dev.txt` includes runtime deps via `-r requirements.txt` plus pytest.

## Queue after restart

- Every **queued** or **running** job is marked `error` / `bot_restart` on the next start.
- Jobs are never replayed automatically: a remote endpoint may have accepted an in-flight row before SQLite recorded it.
- The durable job payload is removed during reconciliation; the original `/data` profile remains on disk.
- The user explicitly picks the project again to create a new job.
- Project `module.py` / `api.py` changes are also detected by the in-process source watcher and hot-reloaded. Waiting jobs then use the new adapter without a user command.
- Core bot changes still require a normal service restart; interrupted jobs will stop with `bot_restart`.

## Concurrency and request limits

- Each project has up to `PROJECT_CONCURRENCY` concurrent jobs (default `3`; this deployment runs `6`).
- VIP users keep `VIP_JOB_PARALLEL=5` row workers per VIP user/job; VIP users do not share one five-worker pool.
- `GLOBAL_HTTP_CONCURRENCY` (default `40`) limits simultaneous HTTP calls.
- `GLOBAL_REQUESTS_PER_MINUTE` (default `100`) limits queue submit admissions across all projects and users.
- These environment overrides require a service restart.
- `HOODHUNTERS_MIN_STEP_DELAY` defaults to `60` seconds because the HoodHunters API rejects tokens submitted before the browser flow's gated steps can be completed.
  **Do not lower it to buy throughput.** Setting `30` on 2026-08-13 turned every
  submit into `{"error":"that was too fast, finish the steps first"}` — 409 rows
  lost, 0 OK, from the restart at 08:20 UTC until it was put back to `60`. The
  adapter now doubles its own wait (up to `HOODHUNTERS_MAX_STEP_DELAY`, default
  `180`) and retries when it sees that answer, so a too-low value costs latency
  rather than rows — but it still costs.

## Backup

Back up the **whole persistent state directory**, not just the SQLite file:
wallet profiles (`user_data/`), per-user proxies and queue payloads live outside
the DB and are exactly the data that must survive a checkout wipe. A DB-only
backup cannot restore wallets that were uploaded but never submitted.

```bash
# Verified hot backup: SQLite .backup + all non-DB state files.
ops/backup_state.sh /root/.autoreg /root/backups
```

The script verifies the SQLite copy with `PRAGMA quick_check`, validates the
compressed tar archive, publishes artifacts atomically, and exits non-zero on
any incomplete backup. `AUTOREG_BACKUP_KEEP` controls retention (default 7).

Do not commit `.env` or anything from the persistent state directory.

## Pruning dead projects from the DB

`ops/prune_db.py` deletes rows belonging to projects the bot no longer serves.
A `project_id` counts as **live** when `projects/<id>/project.toml` exists and
says `enabled = true`; anything else (disabled, deleted from the tree, or an id
that only ever existed in the DB) is prunable once older than the retention
window — 7 days by default.

```bash
ops/prune_db.py                 # dry run: prints the plan, deletes nothing
ops/prune_db.py --apply         # delete, after an automatic verified backup
ops/prune_db.py --days 30 --apply --vacuum
```

Installed as a daily cron at 05:00, logging to `~/.autoreg/prune_db.log`.

Live projects are never pruned at any age: `submissions` backs the global
wallet dedup (`bot/db/stats.py: already_registered_wallet`), so dropping a live project's
history would let already-registered wallets through a second time. For the
same reason, re-adding a project whose rows were pruned starts its dedup from
scratch. Tables covered: `submissions`, `submit_jobs`, `user_project_stats`,
`wallet_claims`. `ok_slot_reservations` is left alone — it is live queue
bookkeeping, cleared on boot, not history.

Safety: refuses to run when no live project is found (guards against a wrong
`--projects` path wiping everything), treats an unreadable folder or a bad
`project.toml` as live, backs the DB up to `tgbot.pre-prune-<stamp>.db` before
its first delete, and keeps the last 3 such backups.

## Payments (NOWPayments, USDT BEP-20)

```bash
# .env — never commit, never back up inside the repo (.env.bak-* is gitignored)
NOWPAYMENTS_API_KEY=...        # empty → 💎 section hidden, poller off
PAYMENTS_ENABLED=1             # 0 → whole payments subsystem off, poller off
SALES_ENABLED=1                # 0 → shop closed: no new payments, existing plans run out
# PAYMENTS_TEST_USER_IDS=123   # rollout allow-list (empty = everyone)
# NOWPAYMENTS_SANDBOX=1        # api-sandbox.nowpayments.io, no real money
```

- Changing any of these needs a **process restart** (env is read at import).
- Startup logs `[np] <base> min=$X ...` and `[pay] poller started (every Ns)`.
- Health: `/admin` → 💳 Платежи shows API reachability, the cached minimum and
  the last payments; 💎 Подписки shows active plans and revenue.
- A payment is credited **only** by `db.credit_payment_if_new`; to fix one by
  hand use `/grant_plan <uid> <basic>`, never a manual DB edit.
- Back up `payments` and `user_subscriptions` with the rest of the state dir —
  they are the record of who paid.

### Closing the shop (`SALES_ENABLED=0`)

Set it, restart, done. What changes and what deliberately does not:

| | with `SALES_ENABLED=0` |
|---|---|
| new payments | blocked — buy buttons gone, `plan:buy:` refuses a stale one |
| existing plans | untouched; they keep their tier until `expires_at` |
| 💎 button | shown to plan holders only, as **Мой тариф** (status, no prices) |
| poller | still running, so a transfer already in flight is still credited |
| `/grant_plan`, `/revoke_plan` | still work — support and compensation are unaffected |
| expiry reminders | switch to the wording without the "renew" call to action |

Keep `PAYMENTS_ENABLED=1` while any payment could still land. Setting *that*
to 0 stops the poller and hides the section from everyone, plan holders too.

## Secrets permissions

```bash
chmod 600 .env proxy.txt
# user proxies are saved as 0600 by the bot
# .env backups belong OUTSIDE the repo, e.g. ~/.autoreg/
```

## Tests

```bash
cd /root/bot-regaauto-clean && source venv/bin/activate
python tests/test_limits_proxies.py
python tests/test_admin_improvements.py
python tests/test_i18n_http.py
```

## Git push (deploy key)

`origin` is an SSH remote and the checkout has no `~/.ssh/config`, so name the
deploy key explicitly. Port 22 is often blocked; GitHub also serves SSH on 443:

```bash
ssh-keyscan -p 443 -t ed25519 ssh.github.com >> ~/.ssh/known_hosts   # once
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_autoreg -o IdentitiesOnly=yes \
  -o Hostname=ssh.github.com -o Port=443' git push origin main
```
