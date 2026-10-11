#!/usr/bin/env bash
# PostgreSQL backup (custom format, compressed) of the monitoring database.
#
#   scripts/backup.sh [label]       # label: nightly | pre-deploy | manual (default)
#
# Env: BACKUP_DIR (default /srv/backups), BACKUP_KEEP_DAYS (default 14)
# Off-host encrypted copies and restore tests are milestone M7
# (scripts/restore-test.sh, docs/disaster-recovery.md).
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$here/deploy/docker"
label="${1:-manual}"
BACKUP_DIR="${BACKUP_DIR:-/srv/backups}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"

db="$(sed -n 's/^POSTGRES_DB=//p' "$compose_dir/.env")"
user="$(sed -n 's/^POSTGRES_USER=//p' "$compose_dir/.env")"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/${db:-monitoring}-$stamp-$label.dump"

install -d -m 750 "$BACKUP_DIR"
cd "$compose_dir"
for _ in $(seq 1 30); do
    docker compose exec -T postgres pg_isready -q -U "${user:-monitoring}" && break
    sleep 2
done
umask 077
docker compose exec -T postgres pg_dump -U "${user:-monitoring}" -d "${db:-monitoring}" -Fc >"$out.partial"
mv "$out.partial" "$out"
echo "backup: $out ($(du -h "$out" | cut -f1))"

find "$BACKUP_DIR" -name "*-nightly.dump" -mtime +"$BACKUP_KEEP_DAYS" -delete
find "$BACKUP_DIR" -name "*-pre-deploy.dump" -mtime +$(( BACKUP_KEEP_DAYS * 4 )) -delete
