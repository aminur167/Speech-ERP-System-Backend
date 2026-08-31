#!/usr/bin/env bash
#
# Nightly PostgreSQL backup for the Speech ERP production database.
#
# Takes a compressed, custom-format pg_dump (restorable with pg_restore,
# including selective table/schema restores -- a plain SQL dump can't do
# that), copies it off-server, then prunes local copies past the retention
# window. Off-server is not optional: a backup that lives on the same disk
# as the database it protects is not a real backup (docs/00 engineering
# checklist).
#
# Run via cron, e.g. daily at 02:15 server time:
#   15 2 * * * /opt/speech-erp/backend/deploy/scripts/backup_db.sh >> /var/log/speech-erp/backup.log 2>&1
#
# Reads DB credentials the same way Django does -- from the deployed env
# file -- so there is exactly one place connection details are configured.
set -euo pipefail

ENV_FILE="${SPEECH_ERP_ENV_FILE:-/etc/speech-erp/backend.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

: "${POSTGRES_DB:?POSTGRES_DB must be set (directly or via $ENV_FILE)}"
: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/speech-erp}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILENAME="speech_erp_${TIMESTAMP}.dump"
LOCAL_PATH="${BACKUP_DIR}/${FILENAME}"

mkdir -p "$BACKUP_DIR"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

echo "[backup_db] Starting dump of '${POSTGRES_DB}' -> ${LOCAL_PATH}"
pg_dump \
  --host="$POSTGRES_HOST" \
  --port="$POSTGRES_PORT" \
  --username="$POSTGRES_USER" \
  --format=custom \
  --compress=9 \
  --file="$LOCAL_PATH" \
  "$POSTGRES_DB"

DUMP_BYTES=$(stat --format=%s "$LOCAL_PATH" 2>/dev/null || stat -f%z "$LOCAL_PATH")
if [[ "$DUMP_BYTES" -lt 1024 ]]; then
  # A near-empty file means pg_dump silently produced garbage (e.g. wrong
  # database, connection dropped mid-dump) -- fail loudly rather than ship
  # a worthless backup off-server and call it a night.
  echo "[backup_db] ERROR: dump is suspiciously small (${DUMP_BYTES} bytes) -- aborting." >&2
  rm -f "$LOCAL_PATH"
  exit 1
fi
echo "[backup_db] Dump complete: ${DUMP_BYTES} bytes"

# --- Off-server copy -------------------------------------------------------
# Configure exactly one of these. Neither set = backups only ever exist on
# the same disk as the database, which does not satisfy "off-server".
if [[ -n "${BACKUP_S3_BUCKET:-}" ]]; then
  echo "[backup_db] Uploading to s3://${BACKUP_S3_BUCKET}/${FILENAME}"
  aws s3 cp "$LOCAL_PATH" "s3://${BACKUP_S3_BUCKET}/${FILENAME}" --only-show-errors
elif [[ -n "${BACKUP_REMOTE_HOST:-}" ]]; then
  REMOTE_DIR="${BACKUP_REMOTE_DIR:-/var/backups/speech-erp}"
  echo "[backup_db] Copying to ${BACKUP_REMOTE_HOST}:${REMOTE_DIR}/"
  rsync -az -e "ssh -i ${BACKUP_REMOTE_SSH_KEY:-$HOME/.ssh/id_ed25519}" \
    "$LOCAL_PATH" "${BACKUP_REMOTE_USER:-backup}@${BACKUP_REMOTE_HOST}:${REMOTE_DIR}/"
else
  echo "[backup_db] WARNING: neither BACKUP_S3_BUCKET nor BACKUP_REMOTE_HOST is set." >&2
  echo "[backup_db] This backup exists only on the local disk -- set one of them." >&2
fi

# --- Local retention ---------------------------------------------------
find "$BACKUP_DIR" -name 'speech_erp_*.dump' -mtime "+${RETENTION_DAYS}" -print -delete

echo "[backup_db] Done."
