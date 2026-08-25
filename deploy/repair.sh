#!/usr/bin/env bash
# Rebuild trading.db around a corrupt table, keeping every other row.
#
#   bash deploy/repair.sh risk_state
#
# For PARTIAL corruption — one broken btree, everything else readable. That
# is what happened on 2026-08-25 (twice), both times to `risk_state`: the
# smallest, hottest, most-rewritten page in the database, and the only table
# written from a GET endpoint the dashboard polls continuously.
#
# Use this INSTEAD of restore.sh when the damage is one table. restore.sh
# rolls the whole database back to 18:30 the previous evening; this keeps
# every row in every healthy table and takes only the named table from the
# backup. On 2026-08-25 that was the difference between losing ~500
# announcements and losing none.
#
# Find the table name from the tree number quick_check reports:
#   sqlite3 data/trading.db "PRAGMA quick_check(3);"
#     -> Tree 35976 page 35976: btreeInitPage() returns error code 11
#   sqlite3 data/trading.db "select name from sqlite_schema where rootpage=35976;"
#
# Only sound for a table you can afford to take from the backup — a config
# singleton like risk_state. For a corrupt `announcements` or `trades`, stop
# and recover those rows by hand first.
#
# `.recover` would be the obvious tool and is NOT available: packaged
# sqlite3 3.45.1 is built without sqlite_dbpage. Hence the per-table dump.

set -euo pipefail

BAD="${1:-}"
[ -n "$BAD" ] || { echo "usage: bash deploy/repair.sh <corrupt-table>" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DB="data/trading.db"
BK="$(ls -1t data/backups/trading-*.db 2>/dev/null | head -1 || true)"
[ -n "$BK" ] && [ -f "$BK" ] || { echo "no backup to source '$BAD' from" >&2; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="/tmp/repair-$STAMP"
mkdir -p "$WORK"

echo "corrupt table: $BAD"
echo "backup:        $BK"

echo "==> Stopping the service"
# Never repair under a live uvicorn: its open fds point at the old inode and
# its WAL gets replayed onto whatever file ends up at that path.
sudo systemctl stop tradebot
sleep 3

echo "==> Copying (all work happens on the copy, never the original)"
cp "$DB" "$WORK/rec.db"
[ -f "$DB-wal" ] && cp "$DB-wal" "$WORK/rec.db-wal"
# Fold the WAL in, or every row committed since the last checkpoint is lost.
sqlite3 "$WORK/rec.db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true

TABLES="$(sqlite3 "$WORK/rec.db" \
    "select name from sqlite_schema where type='table' \
     and name not like 'sqlite_%' and name<>'$BAD' order by name;")"
echo "==> Dumping $(echo "$TABLES" | wc -l) tables, skipping $BAD"
: > "$WORK/tables.sql"
for t in $TABLES; do
    sqlite3 "$WORK/rec.db" ".dump $t" >> "$WORK/tables.sql" 2>>"$WORK/dump.err" \
        || { echo "DUMP FAILED on '$t' — aborting, nothing changed" >&2; exit 1; }
done
# A plain .dump ends in "ROLLBACK; -- due to errors" when it hits a bad page,
# and that silently produces a short database. Refuse it.
if grep -q 'ROLLBACK' "$WORK/tables.sql"; then
    echo "dump ended in ROLLBACK — more than one tree is damaged; aborting" >&2
    exit 1
fi

echo "==> Rebuilding"
sqlite3 "$WORK/fixed.db" < "$WORK/tables.sql"
sqlite3 "$BK" ".dump $BAD" | sqlite3 "$WORK/fixed.db"

echo "==> Restoring indexes/views/triggers"
# A per-table .dump carries that table's own indexes but not views, triggers,
# or anything the skipped table owned. Replay them from the old schema.
sqlite3 "$WORK/rec.db" \
    "select sql from sqlite_schema where type in ('index','view','trigger') \
     and sql is not null;" | sed 's/$/;/' | sqlite3 "$WORK/fixed.db" 2>/dev/null || true

echo "==> Verifying"
CHECK="$(sqlite3 "$WORK/fixed.db" 'PRAGMA integrity_check;')"
[ "$CHECK" = "ok" ] || { echo "REPAIR FAILED: $CHECK — nothing changed" >&2; exit 1; }

# Health is not enough — a database that lost half its rows also reads "ok".
# Compare row counts and schema-object counts against the damaged original.
echo "-- parity: original -> repaired --"
FAIL=0
for t in $TABLES; do
    a="$(sqlite3 "$WORK/rec.db" "select count(*) from $t;" 2>/dev/null || echo ERR)"
    b="$(sqlite3 "$WORK/fixed.db" "select count(*) from $t;" 2>/dev/null || echo ERR)"
    if [ "$a" != "$b" ]; then
        printf "   %-20s %10s -> %10s  MISMATCH\n" "$t" "$a" "$b"; FAIL=1
    fi
done
[ "$FAIL" = "0" ] && echo "   all $(echo "$TABLES" | wc -l) tables match"
for k in table index view trigger; do
    a="$(sqlite3 "$WORK/rec.db" "select count(*) from sqlite_schema where type='$k';")"
    b="$(sqlite3 "$WORK/fixed.db" "select count(*) from sqlite_schema where type='$k';")"
    printf "   %-8s %3s -> %3s %s\n" "${k}s" "$a" "$b" "$([ "$a" = "$b" ] && echo OK || echo MISMATCH)"
    [ "$a" = "$b" ] || FAIL=1
done
[ "$FAIL" = "0" ] || { echo "PARITY FAILED — nothing changed; inspect $WORK" >&2; exit 1; }

echo "==> Swapping in"
# mv, never rm. The damaged file is the only record of anything the dump
# could not reach, and `.dump` improvements may make it readable later.
mv "$DB" "$DB.corrupt-$STAMP"
for side in wal shm; do
    [ -f "$DB-$side" ] && mv "$DB-$side" "$DB-$side.corrupt-$STAMP"
done
cp "$WORK/fixed.db" "$DB"
sqlite3 "$DB" "PRAGMA journal_mode=WAL;" >/dev/null
echo "old db kept at $DB.corrupt-$STAMP"

sudo systemctl start tradebot
sleep 10
systemctl is-active tradebot
echo "post-restart quick_check: $(sqlite3 "$DB" 'PRAGMA quick_check(1);')"
echo "Confirm the row count is CLIMBING before walking away:"
echo "  watch -n5 'sqlite3 $PROJECT_ROOT/$DB \"select count(*) from announcements;\"'"
