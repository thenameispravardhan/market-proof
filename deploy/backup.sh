#!/usr/bin/env bash
# Nightly SQLite backup with 7-day rotation.
#
# Uses sqlite3's online .backup (safe against a live WAL database — no
# need to stop the bot). Install as a weekday cron job after market close
# (server timezone is Asia/Kolkata, so cron times are IST):
#
#   crontab -e
#   30 18 * * 1-5  /home/ubuntu/tradebot/deploy/backup.sh >> /home/ubuntu/tradebot/logs/backup.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DB="$PROJECT_ROOT/data/trading.db"
DEST_DIR="$PROJECT_ROOT/data/backups"

[ -f "$DB" ] || { echo "no database at $DB; nothing to back up"; exit 0; }
mkdir -p "$DEST_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"

# Stage under a DOTTED name first: the rotation glob below is
# `trading-*.db`, so a half-written or corrupt attempt must not carry that
# name until it has been verified. On 2026-08-07 a corrupt source DB left a
# 0-byte `trading-20260807-183001.db` sitting in the rotation set — one more
# night and it would have evicted a GOOD backup to keep an empty one.
TMP="$DEST_DIR/.trading-$STAMP.db.partial"
# Clean the sidecars too: sqlite3 opens the destination in WAL mode, so a
# failed run left `.partial-shm` / `.partial-wal` behind (three pairs were
# still sitting there on 2026-08-25).
trap 'rm -f "$TMP" "$TMP-shm" "$TMP-wal"' EXIT

sqlite3 "$DB" ".backup '$TMP'"

# A backup nobody verified is not a backup. `.backup` against a malformed
# source can exit non-zero *after* creating the file, so check the copy
# itself rather than trusting the exit code.
[ -s "$TMP" ] || { echo "BACKUP FAILED: $TMP is empty (source DB corrupt?)" >&2; exit 1; }
if [ "$(sqlite3 "$TMP" 'PRAGMA integrity_check;' 2>&1)" != "ok" ]; then
    echo "BACKUP FAILED: integrity_check did not return ok — source DB is damaged" >&2
    exit 1
fi

mv "$TMP" "$DEST_DIR/trading-$STAMP.db"
trap - EXIT

# Keep the 7 newest backups. Only verified files ever reach this glob.
ls -1t "$DEST_DIR"/trading-*.db 2>/dev/null | tail -n +8 | xargs -r rm --

echo "$(date -Is) backup written: $DEST_DIR/trading-$STAMP.db"
