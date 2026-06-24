#!/bin/sh
# crond runs jobs with a near-empty environment, so dump the container's env
# (DATABASE_URL, RCLONE_REMOTE, etc.) to a file the cron job sources first.
set -eu

: > /etc/backup.env
env | while IFS='=' read -r key value; do
    printf 'export %s=%s\n' "$key" "$(printf '%s' "$value" | sed "s/'/'\\\\''/g; s/^/'/; s/$/'/")" >> /etc/backup.env
done
chmod 600 /etc/backup.env

SCHEDULE="${BACKUP_CRON_SCHEDULE:-0 3 * * *}"
echo "${SCHEDULE} . /etc/backup.env; /usr/local/bin/backup_postgres.sh >> /proc/1/fd/1 2>> /proc/1/fd/2" > /etc/crontabs/root

exec crond -f -l 8
