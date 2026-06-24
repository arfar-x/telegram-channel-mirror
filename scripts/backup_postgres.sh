#!/bin/sh
# Dumps the Postgres database, uploads the dump to Google Drive via rclone,
# then prunes local and remote dumps older than BACKUP_RETENTION_DAYS.
# Required env: DATABASE_URL, RCLONE_REMOTE (e.g. "gdrive:tg-mirror-backups")
# Optional env: BACKUP_RETENTION_DAYS (default 7), BACKUP_DIR (default /backups)
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
DUMP_FILE="${BACKUP_DIR}/mirror_${TIMESTAMP}.dump.gz"

mkdir -p "$BACKUP_DIR"

echo "[backup] dumping database to ${DUMP_FILE}"
pg_dump --format=custom --dbname="$DATABASE_URL" | gzip > "$DUMP_FILE"

echo "[backup] uploading to ${RCLONE_REMOTE}"
rclone copy "$DUMP_FILE" "$RCLONE_REMOTE" --no-traverse

echo "[backup] pruning local dumps older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -name 'mirror_*.dump.gz' -mtime "+${RETENTION_DAYS}" -delete

echo "[backup] pruning remote dumps older than ${RETENTION_DAYS} days"
rclone delete "$RCLONE_REMOTE" --min-age "${RETENTION_DAYS}d"

echo "[backup] done"
