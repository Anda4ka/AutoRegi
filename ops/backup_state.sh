#!/usr/bin/env bash
# Full backup of the persistent autoreg state directory.
#
# Backs up the SQLite DB (consistent .backup), wallet profiles, per-user
# proxies, queue payloads, subscribers and shared proxies — i.e. everything
# that must survive a checkout wipe. Safe to run while the bot is up.
#
# Usage:
#   ops/backup_state.sh [STATE_DIR] [BACKUP_DIR]
# Cron example (daily 04:30):
#   30 4 * * * /root/bot-regaauto-clean/ops/backup_state.sh >/dev/null 2>&1

set -euo pipefail
umask 077

STATE_DIR="${1:-/root/.autoreg}"
BACKUP_DIR="${2:-/root/backups}"
KEEP="${AUTOREG_BACKUP_KEEP:-7}"
TS="$(date +%F-%H%M%S)"

if [ ! -d "$STATE_DIR" ]; then
    echo "state dir not found: $STATE_DIR" >&2
    exit 1
fi
mkdir -p "$BACKUP_DIR"
TMP_DIR="$(mktemp -d "$BACKUP_DIR/.autoreg-backup-$TS.XXXXXX")"
trap 'rm -rf -- "$TMP_DIR"' EXIT

DB="$STATE_DIR/tgbot.db"
if [ ! -f "$DB" ]; then
    echo "database not found: $DB" >&2
    exit 1
fi
TMP_DB="$TMP_DIR/tgbot-$TS.db"
sqlite3 "$DB" ".backup '$TMP_DB'"
if [ "$(sqlite3 "$TMP_DB" 'PRAGMA quick_check;')" != "ok" ]; then
    echo "SQLite backup verification failed" >&2
    exit 1
fi

# Everything except the live SQLite files. This automatically includes
# bot_results.jsonl and any extra proxy files stored in STATE_DIR.
TMP_TAR="$TMP_DIR/autoreg-files-$TS.tar"
TMP_GZ="$TMP_DIR/autoreg-files-$TS.tar.gz"
tar -C "$STATE_DIR" \
    --exclude='./tgbot.db' \
    --exclude='./tgbot.db-wal' \
    --exclude='./tgbot.db-shm' \
    -cf "$TMP_TAR" .
gzip -c "$TMP_TAR" > "$TMP_GZ"
tar -tzf "$TMP_GZ" >/dev/null

# Publish only verified complete artifacts.
mv "$TMP_DB" "$BACKUP_DIR/tgbot-$TS.db"
mv "$TMP_GZ" "$BACKUP_DIR/autoreg-files-$TS.tar.gz"

chmod -R go-rwx "$BACKUP_DIR"

# Rotate: keep the newest $KEEP file archives + $KEEP DB copies.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'autoreg-files-*.tar.gz' -printf '%T@ %p\n' \
    | sort -rn | tail -n +"$((KEEP + 1))" | cut -d' ' -f2- | xargs -r -d '\n' rm --
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'tgbot-*.db' -printf '%T@ %p\n' \
    | sort -rn | tail -n +"$((KEEP + 1))" | cut -d' ' -f2- | xargs -r -d '\n' rm --

echo "backup ok: $BACKUP_DIR (ts=$TS, kept=$KEEP)"
