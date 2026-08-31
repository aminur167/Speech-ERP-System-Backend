#!/usr/bin/env bash
#
# The "periodically test-restored" half of docs/00's backup requirement.
# A backup nobody has ever restored is a guess, not a guarantee -- this
# restores the most recent dump into a throwaway database, sanity-checks it
# actually has data in the tables that matter, then drops the scratch
# database. Exits non-zero on any failure so cron catches it (mail, or wire
# stderr into the same alerting as the app).
#
# Run weekly, e.g. Sunday 03:00 server time:
#   0 3 * * 0 /opt/speech-erp/backend/deploy/scripts/verify_backup_restore.sh >> /var/log/speech-erp/restore-drill.log 2>&1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/speech-erp}"
SCRATCH_DB="speech_erp_restore_drill"

ENV_FILE="${SPEECH_ERP_ENV_FILE:-/etc/speech-erp/backend.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi
: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

LATEST_DUMP="$(find "$BACKUP_DIR" -name 'speech_erp_*.dump' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
if [[ -z "$LATEST_DUMP" ]]; then
  echo "[verify_backup] ERROR: no backup found in ${BACKUP_DIR}." >&2
  exit 1
fi
echo "[verify_backup] Testing restore of: ${LATEST_DUMP}"

cleanup() {
  dropdb --host="$POSTGRES_HOST" --port="$POSTGRES_PORT" --username="$POSTGRES_USER" \
    --if-exists "$SCRATCH_DB" 2>/dev/null || true
}
trap cleanup EXIT

"$SCRIPT_DIR/restore_db.sh" "$LATEST_DUMP" "$SCRATCH_DB"

echo "[verify_backup] Sanity-checking row counts..."
# A restore that "succeeds" but lands zero rows in core tables is exactly
# the failure mode this drill exists to catch (e.g. dumping the wrong
# database, a truncated/corrupt file that pg_restore tolerates silently).
FAILURES=0
for TABLE in patients_patient payments_payment; do
  COUNT=$(psql --host="$POSTGRES_HOST" --port="$POSTGRES_PORT" --username="$POSTGRES_USER" \
    --dbname="$SCRATCH_DB" --tuples-only --no-align \
    -c "SELECT COUNT(*) FROM ${TABLE};" 2>/dev/null || echo "ERROR")
  echo "[verify_backup]   ${TABLE}: ${COUNT} rows"
  if [[ "$COUNT" == "ERROR" ]]; then
    echo "[verify_backup]   ERROR: could not query ${TABLE} in the restored database." >&2
    FAILURES=$((FAILURES + 1))
  fi
done

if [[ "$FAILURES" -gt 0 ]]; then
  echo "[verify_backup] FAILED -- restore drill found ${FAILURES} problem(s)." >&2
  exit 1
fi

echo "[verify_backup] PASSED -- ${LATEST_DUMP} restores cleanly and has data."
