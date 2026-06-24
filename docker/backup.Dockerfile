FROM alpine:3.20

RUN apk add --no-cache postgresql16-client rclone bash tzdata

COPY scripts/backup_postgres.sh /usr/local/bin/backup_postgres.sh
COPY docker/backup-entrypoint.sh /usr/local/bin/backup-entrypoint.sh
RUN chmod +x /usr/local/bin/backup_postgres.sh /usr/local/bin/backup-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/backup-entrypoint.sh"]
