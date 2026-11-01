#!/bin/sh
# Dumps the Postgres database, uploads the dump to Google Drive via rclone,
# then prunes local and remote dumps older than BACKUP_RETENTION_DAYS.
# Required env: DATABASE_URL
# Optional env: RCLONE_REMOTE (e.g. "gdrive:tg-mirror-backups"). If unset, the
#   dump is kept locally only and the upload/remote-prune steps are skipped.
# Optional env: BACKUP_RETENTION_DAYS (default 7), BACKUP_DIR (default /backups)
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
DUMP_FILE="${BACKUP_DIR}/mirror_${TIMESTAMP}.dump.gz"

RCLONE_REMOTE="${RCLONE_REMOTE:-}"
if [ -z "$RCLONE_REMOTE" ]; then
    echo "[backup] WARNING: RCLONE_REMOTE is not set — keeping the dump locally only (no off-site copy). Set it in .env, e.g. RCLONE_REMOTE=gdrive:tg-mirror-backups" >&2
fi

mkdir -p "$BACKUP_DIR"

echo "[backup] dumping database to ${DUMP_FILE}"
pg_dump --format=custom --dbname="$DATABASE_URL" | gzip > "$DUMP_FILE"

if [ -n "$RCLONE_REMOTE" ]; then
    echo "[backup] uploading to ${RCLONE_REMOTE}"
    rclone copy "$DUMP_FILE" "$RCLONE_REMOTE" --no-traverse
fi

echo "[backup] pruning local dumps older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -name 'mirror_*.dump.gz' -mtime "+${RETENTION_DAYS}" -delete

if [ -n "$RCLONE_REMOTE" ]; then
    echo "[backup] pruning remote dumps older than ${RETENTION_DAYS} days"
    rclone delete "$RCLONE_REMOTE" --min-age "${RETENTION_DAYS}d"
fi

echo "[backup] done"
