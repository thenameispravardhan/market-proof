#!/usr/bin/env bash
# Restore the live SQLite DB from the newest VERIFIED nightly backup.
#
# The corruption runbook, as a script. Ran by hand three times now
# (2026-08-07, 2026-08-25 x2) and got it slightly different each time —
# once leaving a stale -wal beside a restored .db, which re-corrupts on
# the next open. The ordering below is the part that matters:
#
#   stop the service  ->  move the old .db AND its -wal/-shm aside
#   ->  copy the backup in  ->  verify  ->  start
#
# Never restore under a running uvicorn: its open file descriptors still
# point at the old inode, and its WAL will be replayed onto the new file.
#
#   bash deploy/restore.sh              # newest verified backup
#   bash deploy/restore.sh <path.db>    # a specific one

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DB="$PROJECT_ROOT/data/trading.db"
DEST_DIR="$PROJECT_ROOT/data/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"

SRC="${1:-}"
if [ -z "$SRC" ]; then
    SRC="$(ls -1t "$DEST_DIR"/trading-*.db 2>/dev/null | head -1 || true)"
fi
[ -n "$SRC" ] && [ -f "$SRC" ] || { echo "no backup found in $DEST_DIR" >&2; exit 1; }

# Verify BEFORE touching anything live. A restore from a bad backup is
# strictly worse than the corrupt DB you already have — at least that one
# still serves reads.
echo "source:  $SRC"
CHECK="$(sqlite3 "$SRC" 'PRAGMA integrity_check;' 2>&1)"
[ "$CHECK" = "ok" ] || { echo "REFUSING: backup is damaged ($CHECK)" >&2; exit 1; }
echo "rows:    $(sqlite3 "$SRC" 'select count(*) from announcements;') announcements, "\
"latest $(sqlite3 "$SRC" 'select max(received_at) from announcements;') UTC"

sudo systemctl stop tradebot
sleep 2

# Keep the corrupt copy — it is the only record of the rows written since
# the backup, and `sqlite3 .recover` can often pull them back out later.
mv "$DB" "$DB.corrupt-$STAMP"
for side in wal shm; do
    [ -f "$DB-$side" ] && mv "$DB-$side" "$DB-$side.corrupt-$STAMP"
done
echo "old db kept at $DB.corrupt-$STAMP"

cp "$SRC" "$DB"
FINAL="$(sqlite3 "$DB" 'PRAGMA integrity_check;' 2>&1)"
[ "$FINAL" = "ok" ] || { echo "RESTORE FAILED: copy reads as $FINAL" >&2; exit 1; }

sudo systemctl start tradebot
sleep 8
systemctl is-active tradebot
echo "restored. The monitors re-ingest the gap on their own — announcements"
echo "are re-fetched by content hash, so nothing double-inserts."
