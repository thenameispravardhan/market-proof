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
sqlite3 "$DB" ".backup '$DEST_DIR/trading-$STAMP.db'"

# Keep the 7 newest backups.
ls -1t "$DEST_DIR"/trading-*.db 2>/dev/null | tail -n +8 | xargs -r rm --

echo "$(date -Is) backup written: $DEST_DIR/trading-$STAMP.db"
