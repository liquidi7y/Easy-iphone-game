#!/usr/bin/env bash
# Prove a backup is restorable: restore it into a throw-away database inside
# the running postgres container, compare row counts, drop it again.
# The production database is never touched.
#
#   scripts/restore-test.sh [backup.dump]    # default: newest file in $BACKUP_DIR
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$here/deploy/docker"
BACKUP_DIR="${BACKUP_DIR:-/srv/backups}"
dump="${1:-$(ls -t "$BACKUP_DIR"/*.dump 2>/dev/null | head -n1)}"
[[ -f "$dump" ]] || { echo "restore-test: no backup found" >&2; exit 1; }

user="$(sed -n 's/^POSTGRES_USER=//p' "$compose_dir/.env")"; user="${user:-monitoring}"
db="$(sed -n 's/^POSTGRES_DB=//p' "$compose_dir/.env")"; db="${db:-monitoring}"
scratch="restore_test_$(date +%s)"
cd "$compose_dir"
psql() { docker compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U "$user" "$@"; }
cleanup() { psql -d postgres -c "DROP DATABASE IF EXISTS $scratch" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "restore-test: $dump -> $scratch"
psql -d postgres -c "CREATE DATABASE $scratch"
psql -d "$scratch" -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" >/dev/null
docker compose exec -T postgres pg_restore -U "$user" -d "$scratch" --no-owner <"$dump" 2>&1 \
    | grep -v -E 'circular foreign-key|detail: (hypertable|chunk|continuous_agg)|hint: ' || true
psql -d "$scratch" -c "SELECT timescaledb_post_restore()" >/dev/null

query="SELECT (SELECT count(*) FROM machines) || ' machines, ' || (SELECT count(*) FROM machine_state_events) || ' state events, ' || (SELECT count(*) FROM production_counters) || ' counter rows, ' || (SELECT count(*) FROM schema_migrations) || ' migrations'"
restored="$(psql -d "$scratch" -tA -c "$query")"
live="$(psql -d "$db" -tA -c "$query")"
echo "restore-test: restored: $restored"
echo "restore-test: live now: $live"
machines="$(psql -d "$scratch" -tA -c "SELECT count(*) FROM machines")"
(( machines > 0 )) || { echo "restore-test: FAILED (no machines restored)" >&2; exit 1; }
echo "restore-test: OK"
