#!/usr/bin/env bash
# Local end-to-end test of the App Service container image.
#
#   bash scripts/local-e2e.sh            build the image, start it, run checks, leave it running
#   bash scripts/local-e2e.sh --down     same, then stop and remove it
#   bash scripts/local-e2e.sh --no-build reuse the existing crip:local image
#
# Works in Linux, macOS, WSL and Git Bash on Windows. Needs Docker (BuildKit +
# compose v2) and curl. Run from anywhere; it cds to the repo root.
#
# Proves without any Azure access: the image builds; it runs as non-root; the
# UI, /config.js and SPA routing are served by the same container as the API;
# unauthenticated chat gets the typed 401 envelope; the SQLite store and its
# grounding CHECK constraint are created; all domain agents are exposed;
# agent registration payloads render from inside the image.
#
# Cannot prove locally: a real answer. That needs Entra sign-in, Foundry and
# Azure access; the app deliberately has no mock mode. With a real .env and
# .secrets/obo-client-secret the same container is wired to your tenant.
#
# Optional: DOCKER=/path/to/docker, NPM_MAXSOCKETS (default 4 locally).

set -euo pipefail
export MSYS_NO_PATHCONV=1          # Git Bash: don't rewrite /app/... paths passed to docker
export DOCKER_BUILDKIT=1
cd "$(dirname "$0")/.."

DOWN=0; BUILD=1
for arg in "$@"; do
  case "$arg" in
    --down) DOWN=1 ;;
    --no-build) BUILD=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

DOCKER="${DOCKER:-docker}"
BASE=http://localhost:8000
compose() { "$DOCKER" compose "$@"; }
PASS=0; FAIL=0
ok()    { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad()   { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
step()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
check() { local name="$1"; shift; if "$@" > /dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
mkdir -p .local .secrets
fetch() { curl -sS -o ".local/$1" -w '%{http_code}' "${@:2}" || true; }   # body -> .local/<file>, prints status

step "Preflight"
command -v "$DOCKER" > /dev/null || { echo "docker not found (set DOCKER=/path/to/docker)"; exit 1; }
"$DOCKER" info > /dev/null 2>&1 || { echo "Docker engine not reachable. Is Docker Desktop running?"; exit 1; }
command -v curl > /dev/null || { echo "curl is required"; exit 1; }
[ -s .secrets/local-postgres-password ] || printf '%s' "local-only-$RANDOM$RANDOM" > .secrets/local-postgres-password
if [ -f .env ]; then MODE="configured (.env)"; else MODE="smoke (placeholder IDs, no Azure)"; fi
echo "docker $("$DOCKER" version --format '{{.Server.Version}}'), mode: $MODE"

if [ "$BUILD" -eq 1 ]; then
  step "Build image (same Dockerfile the pipeline builds)"
  "$DOCKER" build --build-arg NPM_MAXSOCKETS="${NPM_MAXSOCKETS:-4}" -t crip:local .
fi

step "Start container (waits for the health check)"
compose up -d --no-build --wait --wait-timeout 180 app || { echo "container failed to become healthy:"; compose logs --tail 80 app; exit 1; }
compose ps app

step "Checks"
# Image / runtime
check "runs as non-root (uid 10001)" test "$(compose exec -T app id -u | tr -d '\r')" = "10001"
check "no .env or .secrets baked into the image" bash -c "! \"$DOCKER\" run --rm --entrypoint sh crip:local -c 'ls /app/.env /app/.secrets/* 2>/dev/null | grep -q .'"
check "web UI built into the image" bash -c "\"$DOCKER\" run --rm --entrypoint sh crip:local -c 'test -f /app/static/index.html'"

# Health
check "GET /health = ok (store reachable)" bash -c "[ \"\$(curl -fsS $BASE/health)\" = '{\"status\":\"ok\"}' ]"
check "GET /health/live" curl -fsS "$BASE/health/live"

# Web UI from the same origin
check "GET / serves the SPA" bash -c "curl -fsS $BASE/ | grep -q 'id=\"root\"'"
check "GET /config.js built from settings" bash -c "curl -fsS $BASE/config.js | grep -q 'access_as_user'"
check "deep link falls back to index.html" bash -c "curl -fsS $BASE/some/deep/link | grep -q 'id=\"root\"'"
asset="$(curl -fsS "$BASE/" | grep -o '/assets/[^"]*\.js' | head -n1 || true)"
check "hashed JS asset served ($asset)" bash -c "[ -n '$asset' ] && curl -fsS $BASE$asset > /dev/null"
check "security headers present" bash -c "curl -sSI $BASE/ | grep -qi '^x-content-type-options: nosniff'"

# API
code="$(fetch caps.json "$BASE/api/capabilities")"
check "GET /api/capabilities lists all 6 domain agents" bash -c "[ $code = 200 ] && for a in costpulse optimizer inventory governance netdiag platform; do grep -q \$a .local/caps.json || exit 1; done"
code="$(fetch chat-unauth.json -X POST -H 'Content-Type: application/json' -d '{"message":"ping"}' "$BASE/api/chat")"
check "POST /api/chat without token -> 401 envelope" bash -c "[ $code = 401 ] && grep -q '\"unauthenticated\"' .local/chat-unauth.json && grep -q correlation_id .local/chat-unauth.json"
code="$(fetch chat-bad.json -X POST -H 'Authorization: Bearer forged.token.value' -H 'Content-Type: application/json' -d '{"message":"ping"}' "$BASE/api/chat")"
check "POST /api/chat with forged token -> 401 envelope" bash -c "[ $code = 401 ] && grep -q '\"unauthenticated\"' .local/chat-bad.json"
code="$(fetch tool-unauth.json -X POST -d '{}' "$BASE/api/tools/optimizer_find_idle_resources")"
check "tool endpoint requires auth (401)" test "$code" = "401"
code="$(fetch unknown.json "$BASE/api/nope")"
check "unknown /api path -> 404 envelope, not HTML" bash -c "[ $code = 404 ] && grep -q '\"not_found\"' .local/unknown.json"

# Persistence (SQLite in the container's /home/data volume)
tables="$(compose exec -T app python -c "import sqlite3; c=sqlite3.connect('/home/data/crip.db'); print(','.join(r[0] for r in c.execute(\"select name from sqlite_master where type='table' order by name\")))" | tr -d '\r' || true)"
check "SQLite schema applied ($tables)" test "$tables" = "access_log,agent_invocations,messages,sessions"
check "grounding CHECK constraint present" bash -c "compose(){ \"$DOCKER\" compose \"\$@\"; }; compose exec -T app python -c \"import sqlite3; s=sqlite3.connect('/home/data/crip.db').execute(\\\"select sql from sqlite_master where name='agent_invocations'\\\").fetchone()[0]; assert 'grounded_rows_have_provenance' in s\""

# Foundry registration payloads (no Azure call with --dry-run)
agents="$(compose exec -T app python /app/foundry/register_agents.py --dry-run | tr -d '\r' | grep -c '"name": "crip-' || true)"
check "register_agents --dry-run renders 7 agents from the image" test "$agents" -eq 7

step "Result"
echo "passed: $PASS   failed: $FAIL   (mode: $MODE)"
echo "App:   $BASE   (sign-in needs a real app registration in .env)"
echo "API:   $BASE/docs"
echo "Logs:  docker compose logs -f app"
if [ "$DOWN" -eq 1 ]; then
  step "Tear down"; compose down
else
  echo "Stop:  docker compose down        (add -v to also delete the SQLite volume)"
fi
[ "$FAIL" -eq 0 ]
