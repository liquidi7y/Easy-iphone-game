#!/bin/sh
# Apply database/migrations/*.sql in order, once each, and (re)create the
# read-only Grafana role. Safe to re-run. Runs inside the `migrate` compose
# service (TimescaleDB image, so psql is available).
#
# Required env: PGHOST PGUSER PGPASSWORD PGDATABASE GRAFANA_DB_PASSWORD
# Optional env: MIGRATIONS_DIR (default /migrations)
set -eu

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"
PSQL="psql -X -q -v ON_ERROR_STOP=1"

echo "migrate: waiting for database ${PGHOST}..."
i=0
until pg_isready -q; do
    i=$((i + 1))
    [ "$i" -ge 60 ] && { echo "migrate: database not ready after 60s" >&2; exit 1; }
    sleep 1
done

$PSQL -c "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"

for f in $(ls "$MIGRATIONS_DIR"/*.sql | sort); do
    v=$(basename "$f" .sql)
    applied=$($PSQL -tA -c "SELECT 1 FROM schema_migrations WHERE version = '$v'")
    if [ "$applied" = "1" ]; then
        echo "migrate: $v already applied"
        continue
    fi
    echo "migrate: applying $v"
    # One transaction per file: either the migration and its bookkeeping row land, or neither.
    { cat "$f"; echo; echo "INSERT INTO schema_migrations (version) VALUES ('$v');"; } | $PSQL -1 -f -
done

# Read-only role for Grafana (password from the generated .env).
$PSQL -v pw="$GRAFANA_DB_PASSWORD" -v db="$PGDATABASE" <<'SQL'
SELECT format('CREATE ROLE grafana_ro LOGIN PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') \gexec
SELECT format('ALTER ROLE grafana_ro WITH LOGIN PASSWORD %L', :'pw') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO grafana_ro', :'db') \gexec
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
SQL

echo "migrate: done"
