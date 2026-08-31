#!/usr/bin/env bash
#
# Restores a pg_dump custom-format backup (see backup_db.sh) into a
# PostgreSQL database. Used two ways:
#
#   1. Real disaster recovery: restore into the actual production database.
#   2. The scheduled restore drill (verify_backup_restore.sh calls this):
#      restore into a throwaway database to prove the backup is actually
#      restorable, not just that pg_dump exited 0. "An untested backup is
#      not a real backup" (docs/00) -- this script is what makes the test
#      possible, not just theoretical.
#
# Usage: restore_db.sh <dump-file> [target-db-name]
#   target-db-name defaults to POSTGRES_DB from the env file. Restoring into
#   that name (i.e. the live database) requires typing CONFIRM, since this
#   drops and replaces its contents.
set -euo pipefail

DUMP_FILE="${1:?Usage: restore_db.sh <dump-file> [target-db-name]}"
if [[ ! -f "$DUMP_FILE" ]]; then
  echo "[restore_db] ERROR: dump file not found: $DUMP_FILE" >&2
  exit 1
fi

ENV_FILE="${SPEECH_ERP_ENV_FILE:-/etc/speech-erp/backend.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"

TARGET_DB="${2:-${POSTGRES_DB:?POSTGRES_DB must be set if target-db-name is omitted}}"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

if [[ "$TARGET_DB" == "${POSTGRES_DB:-}" ]]; then
  echo "This will DROP and REPLACE the live database '${TARGET_DB}'."
  read -r -p "Type CONFIRM to proceed: " answer
  [[ "$answer" == "CONFIRM" ]] || { echo "Aborted."; exit 1; }
fi

echo "[restore_db] Recreating database '${TARGET_DB}'..."
dropdb --host="$POSTGRES_HOST" --port="$POSTGRES_PORT" --username="$POSTGRES_USER" \
  --if-exists "$TARGET_DB"
createdb --host="$POSTGRES_HOST" --port="$POSTGRES_PORT" --username="$POSTGRES_USER" \
  --owner="$POSTGRES_USER" "$TARGET_DB"

echo "[restore_db] Restoring ${DUMP_FILE} into '${TARGET_DB}'..."
pg_restore \
  --host="$POSTGRES_HOST" \
  --port="$POSTGRES_PORT" \
  --username="$POSTGRES_USER" \
  --dbname="$TARGET_DB" \
  --no-owner \
  --jobs=4 \
  "$DUMP_FILE"

echo "[restore_db] Restore complete into '${TARGET_DB}'."
