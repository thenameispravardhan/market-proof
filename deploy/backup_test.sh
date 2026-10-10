#!/usr/bin/env bash
# Check for deploy/backup.sh: a failed or corrupt backup must NEVER land in
# the rotation set. Drives the real script with a stub `sqlite3`.
set -uo pipefail

REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- stub sqlite3 -------------------------------------------------------
# MODE picks which failure the stub simulates.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/sqlite3" <<'STUB'
#!/usr/bin/env bash
db="$1"; cmd="$2"
case "$cmd" in
  "VACUUM INTO"*)
    out="$(printf '%s' "$cmd" | sed "s/^VACUUM INTO '//; s/'$//")"
    case "$MODE" in
      # Reproduces 2026-08-07: exit non-zero but leave a 0-byte file behind.
      corrupt) : > "$out"; echo "Error: database disk image is malformed" >&2; exit 1 ;;
      # Exits 0 but the copy is garbage -- exit code alone would pass this.
      silent)  echo "garbage" > "$out"; exit 0 ;;
      ok)      echo "data" > "$out"; exit 0 ;;
    esac ;;
  *integrity_check*)
    [ "$MODE" = ok ] && echo "ok" || echo "*** in database main ***"; exit 0 ;;
esac
STUB
chmod +x "$WORK/bin/sqlite3"
export PATH="$WORK/bin:$PATH"

run_case() {
    local mode="$1" expect="$2"
    local root="$WORK/$mode"
    mkdir -p "$root/deploy" "$root/data/backups"
    cp "$REPO/deploy/backup.sh" "$root/deploy/"
    echo "livedb" > "$root/data/trading.db"
    # A good backup already on disk -- it must survive every failure case.
    echo "precious" > "$root/data/backups/trading-20260806-183002.db"

    MODE="$mode" bash "$root/deploy/backup.sh" >/dev/null 2>&1
    local rc=$?

    # Only verified backups may carry the rotation-glob name.
    local n; n=$(ls -1 "$root"/data/backups/trading-*.db 2>/dev/null | wc -l)
    local empties; empties=$(find "$root/data/backups" -name 'trading-*.db' -empty | wc -l)
    local survived=0
    [ -s "$root/data/backups/trading-20260806-183002.db" ] && survived=1

    if [ "$n" = "$expect" ] && [ "$empties" = 0 ] && [ "$survived" = 1 ]; then
        echo "PASS  mode=$mode rc=$rc backups=$n empty=$empties prior_backup_intact=$survived"
    else
        echo "FAIL  mode=$mode rc=$rc backups=$n (want $expect) empty=$empties (want 0) prior_backup_intact=$survived (want 1)"
        FAILED=1
    fi
}

# --- stub aws (offsite upload) -------------------------------------------
cat > "$WORK/bin/aws" <<'STUB'
#!/usr/bin/env bash
[ "$AWS_MODE" = fail ] && { echo "upload denied" >&2; exit 1; }
src="${@: -2:1}"; [ -s "$src" ] || exit 1
echo "$@" >> "$AWS_LOG"; exit 0
STUB
chmod +x "$WORK/bin/aws"

offsite_case() {
    local aws_mode="$1" expect="$2"
    local root="$WORK/offsite-$aws_mode"
    mkdir -p "$root/deploy" "$root/data/backups"
    cp "$REPO/deploy/backup.sh" "$root/deploy/"
    echo "livedb" > "$root/data/trading.db"
    printf 'OTHER=1\nBACKUP_S3_URI="s3://bucket/tradebot/"\n' > "$root/.env"
    MODE=ok AWS_MODE="$aws_mode" AWS_LOG="$root/aws.log" \
        bash "$root/deploy/backup.sh" >/dev/null 2>&1
    local rc=$?
    local got; got=$(sed -n 's/.*"offsite": "\([a-z]*\)".*/\1/p' "$root/data/backups/status.json" 2>/dev/null)
    local n; n=$(ls -1 "$root"/data/backups/trading-*.db 2>/dev/null | wc -l)
    local stray; stray=$(ls -1a "$root/data/backups" | grep -c '\.gz$')
    if [ "$rc" = 0 ] && [ "$got" = "$expect" ] && [ "$n" = 1 ] && [ "$stray" = 0 ]; then
        echo "PASS  offsite aws=$aws_mode status=$got local_backups=$n"
    else
        echo "FAIL  offsite aws=$aws_mode rc=$rc status=$got (want $expect) local_backups=$n stray_gz=$stray"
        FAILED=1
    fi
}

FAILED=0
run_case corrupt 1   # VACUUM INTO fails  -> only the pre-existing backup remains
run_case silent  1   # VACUUM INTO "succeeds" but copy is corrupt -> rejected
run_case ok      2   # healthy        -> new backup joins the old one
offsite_case ok   ok      # upload works        -> status.json says ok
offsite_case fail failed  # upload refused      -> local backup kept, status failed
exit $FAILED
