#!/usr/bin/env bash
# Nightly SQLite backup: 7 verified local copies + one OFFSITE copy.
#
# Uses `VACUUM INTO` (safe against a live WAL database — no need to stop
# the bot). deploy/setup.sh installs it as a weekday cron job after market
# close (server timezone is Asia/Kolkata, so cron times are IST):
#
#   30 18 * * 1-5  /home/ubuntu/tradebot/deploy/backup.sh >> /home/ubuntu/tradebot/logs/backup.log 2>&1
#
# Offsite: the local copies share a disk with the live DB, so a dead disk
# or a deleted instance takes the backups with it. Set BACKUP_S3_URI in
# .env (e.g. s3://my-bucket/tradebot) and install the AWS CLI with
# credentials that can s3:PutObject there; every verified backup is then
# uploaded gzip-compressed. Retention offsite is the bucket's lifecycle
# rule, not this script. A failed upload does NOT fail the local backup —
# it is recorded in data/backups/status.json, which the 08:45 preflight
# reads and alarms on.

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

# `VACUUM INTO` rather than `.backup`. The online-backup API copies
# page-by-page and RESTARTS THE WHOLE COPY every time a writer touches the
# source — on a live 160 MB DB during a news burst that turns a 3-second
# job into a minutes-long read-lock fight (2026-08-14 took 29s; on
# 2026-08-25 the DB corrupted 90 seconds after the run). VACUUM INTO takes
# one read snapshot and writes once, so it cannot loop, and the output is
# compacted for free.
sqlite3 "$DB" "VACUUM INTO '$TMP'"

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

# ---- offsite copy -------------------------------------------------------
# BACKUP_S3_URI comes from the environment or, for cron, from .env (cron
# runs with an empty environment). Only that one key is read from .env —
# the file is not sourced, so nothing else in it is executed.
if [ -z "${BACKUP_S3_URI:-}" ] && [ -f "$PROJECT_ROOT/.env" ]; then
    BACKUP_S3_URI="$(grep -E '^BACKUP_S3_URI=' "$PROJECT_ROOT/.env" | tail -n 1 | cut -d= -f2- | tr -d "\"' \r")"
fi

OFFSITE="disabled"
OFFSITE_AT=""
if [ -n "${BACKUP_S3_URI:-}" ]; then
    if ! command -v aws >/dev/null 2>&1; then
        echo "OFFSITE FAILED: BACKUP_S3_URI is set but the aws CLI is not installed" >&2
        OFFSITE="failed"
    else
        GZ="$DEST_DIR/.trading-$STAMP.db.gz"
        if gzip -c "$DEST_DIR/trading-$STAMP.db" > "$GZ" \
            && aws s3 cp --only-show-errors "$GZ" "${BACKUP_S3_URI%/}/trading-$STAMP.db.gz"; then
            OFFSITE="ok"
            OFFSITE_AT="$(date -Is)"
            echo "$OFFSITE_AT offsite copy uploaded: ${BACKUP_S3_URI%/}/trading-$STAMP.db.gz"
        else
            echo "OFFSITE FAILED: upload to $BACKUP_S3_URI did not complete" >&2
            OFFSITE="failed"
        fi
        rm -f "$GZ"
    fi
fi

# Machine-readable result for the app (GET /api/system/backups and the
# preflight). Written via a temp file so a reader never sees half of it.
cat > "$DEST_DIR/.status.json.tmp" <<JSON
{"local_at": "$(date -Is)", "local_file": "trading-$STAMP.db", "offsite": "$OFFSITE", "offsite_at": "$OFFSITE_AT", "offsite_target": "${BACKUP_S3_URI:-}"}
JSON
mv "$DEST_DIR/.status.json.tmp" "$DEST_DIR/status.json"
