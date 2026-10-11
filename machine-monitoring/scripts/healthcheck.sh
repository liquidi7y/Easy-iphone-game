#!/usr/bin/env bash
# Verify the stack end to end. Exit 0 = healthy.
#
#   scripts/healthcheck.sh               # check once
#   scripts/healthcheck.sh --wait 180    # retry for up to 180 s (used after deploy)
#
# Env: BASE_URL (default http://127.0.0.1:${HTTP_PORT:-80})
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$here/deploy/docker"
wait_s=0
[[ "${1:-}" == "--wait" ]] && wait_s="${2:-120}"

port="$(sed -n 's/^HTTP_PORT=//p' "$compose_dir/.env" 2>/dev/null)"
BASE_URL="${BASE_URL:-http://127.0.0.1:${port:-80}}"
profiles="$(sed -n 's/^COMPOSE_PROFILES=//p' "$compose_dir/.env" 2>/dev/null)"

check() {
    local failures=0 out
    pass() { printf '  ok   %s\n' "$*"; }
    fail() { printf '  FAIL %s\n' "$*"; failures=$((failures + 1)); }

    # Containers: every running service must be healthy where a healthcheck exists.
    out="$(cd "$compose_dir" && docker compose ps --format '{{.Service}} {{.State}} {{.Health}}' 2>&1)" \
        || { fail "docker compose ps: $out"; return 1; }
    while read -r svc state health; do
        [[ -z "$svc" ]] && continue
        if [[ "$state" != "running" ]]; then fail "$svc is $state"
        elif [[ -n "$health" && "$health" != "healthy" ]]; then fail "$svc is $health"
        else pass "$svc ${health:-running}"; fi
    done <<<"$out"

    curl -fs -m 5 "$BASE_URL/healthz" >/dev/null && pass "reverse proxy" || fail "reverse proxy $BASE_URL/healthz"
    curl -fs -m 5 "$BASE_URL/grafana/api/health" >/dev/null && pass "grafana" || fail "grafana"
    if out="$(curl -fs -m 5 "$BASE_URL/api/v1/health")"; then
        pass "api + database"
        if [[ ",$profiles," == *",simulator,"* ]]; then
            # The simulated machine proves the whole path: simulator -> collector -> DB -> API.
            if out="$(curl -fs -m 5 "$BASE_URL/api/v1/machines/SIM01/state")" \
                && grep -q '"collector_reporting":true' <<<"$out" && grep -q '"plc_connected":true' <<<"$out" \
                && ! grep -q '"state":"UNKNOWN"' <<<"$out"; then
                pass "end-to-end: SIM01 state $(sed -n 's/.*"state":"\([A-Z_]*\)".*/\1/p' <<<"$out")"
            else
                fail "end-to-end: SIM01 not reporting a known state yet"
            fi
        fi
    else
        fail "api $BASE_URL/api/v1/health"
    fi
    return "$failures"
}

deadline=$(( $(date +%s) + wait_s ))
while true; do
    report="$(check)"; rc=$?
    if (( rc == 0 )) || (( $(date +%s) >= deadline )); then
        echo "$report"
        if (( rc == 0 )); then echo "healthy"; exit 0; fi
        echo "UNHEALTHY ($rc problem(s))"; exit 1
    fi
    sleep 5
done
