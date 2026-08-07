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
  .backup*)
    out="$(printf '%s' "$cmd" | sed "s/^\.backup '//; s/'$//")"
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

FAILED=0
run_case corrupt 1   # .backup fails  -> only the pre-existing backup remains
run_case silent  1   # .backup "succeeds" but copy is corrupt -> rejected
run_case ok      2   # healthy        -> new backup joins the old one
exit $FAILED
