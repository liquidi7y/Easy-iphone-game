#!/usr/bin/env bash
# Create/complete deploy/docker/.env from .env.example.
#  * keys already present in .env keep their value (secrets are never rotated here)
#  * missing keys are appended; "__GENERATE__" becomes a random 32-char secret
# Usage: scripts/gen-env.sh [path/to/.env]   (env overrides: KEY=value scripts/gen-env.sh)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
example="$here/deploy/docker/.env.example"
target="${1:-$here/deploy/docker/.env}"

gen() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || true; }

umask 077
touch "$target"
chmod 600 "$target"
added=0
while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[A-Z0-9_]+= ]] || continue
    key="${line%%=*}"
    default="${line#*=}"
    if grep -q "^${key}=" "$target"; then
        continue
    fi
    value="${!key:-$default}"           # allow overriding from the environment
    [[ "$value" == "__GENERATE__" ]] && value="$(gen)"
    printf '%s=%s\n' "$key" "$value" >>"$target"
    added=$((added + 1))
done <"$example"

echo "gen-env: $target ready ($added key(s) added)"
