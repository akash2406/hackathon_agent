#!/usr/bin/env bash
# Deploy CRIP's code (API + UI in one zip) to the App Service web app, then smoke-test it.
#
#   az login
#   bash scripts/deploy-appservice.sh --resource-group rg-crip-dev
#   bash scripts/deploy-appservice.sh --resource-group rg-crip-dev --name crip-dev-abc123 --package .local/crip-app.zip
#
# Options:
#   --resource-group, -g   resource group of the web app (required)
#   --name, -n             web app name. Default: output webAppName of the Bicep deployment
#   --deployment           Bicep deployment name to read outputs from (default crip-appservice)
#   --package              an existing zip (from scripts/build_package.py). Default: build one now
#   --skip-ui-build        when building: reuse frontend/dist instead of npm ci + build
#   --no-smoke-test        skip the post-deploy checks
#
# What happens: the zip is uploaded with `az webapp deploy`; App Service's build
# (Oryx, SCM_DO_BUILD_DURING_DEPLOYMENT=true, set by the Bicep template) installs
# requirements.txt from PyPI, then the app starts with the startup command from
# Bicep. The smoke test waits for /health, checks the UI and that the API
# answers an unauthenticated chat with the typed 401.
#
# Runs in Linux, macOS, WSL and Git Bash on Windows. Needs the Azure CLI, Python
# 3.11+ and (when building) Node.js 20.19+/22. No secrets are read or written.

set -euo pipefail
cd "$(dirname "$0")/.."

RG=""; APP=""; DEPLOYMENT="crip-appservice"; PACKAGE=""; SKIP_UI=0; SMOKE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --resource-group|-g) RG="$2"; shift 2 ;;
    --name|-n) APP="$2"; shift 2 ;;
    --deployment) DEPLOYMENT="$2"; shift 2 ;;
    --package) PACKAGE="$2"; shift 2 ;;
    --skip-ui-build) SKIP_UI=1; shift ;;
    --no-smoke-test) SMOKE=0; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$RG" ] || { echo "usage: $0 --resource-group <rg> [--name <web app>] [--package <zip>]  (see --help)" >&2; exit 2; }

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" > /dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2> /dev/null; then PY="$candidate"; break; fi
  done
fi
[ -n "$PY" ] || { echo "Python 3.11+ not found (set PYTHON=/path/to/python)" >&2; exit 1; }
command -v az > /dev/null || { echo "Azure CLI (az) not found" >&2; exit 1; }
az account show --query id -o tsv > /dev/null 2>&1 || { echo "Not signed in to Azure: run 'az login' first" >&2; exit 1; }

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- resolve the web app
if [ -z "$APP" ]; then
  APP="$(az deployment group show -g "$RG" -n "$DEPLOYMENT" --query properties.outputs.webAppName.value -o tsv 2> /dev/null || true)"
  [ -n "$APP" ] || { echo "No --name given and no Bicep deployment '$DEPLOYMENT' in $RG to read it from." >&2; exit 1; }
fi
HOST="$(az webapp show -g "$RG" -n "$APP" --query defaultHostName -o tsv)"
URL="https://$HOST"
echo "Web app: $APP ($URL)"

# The Bicep template configures these; verify so a hand-made web app fails clearly instead of starting nothing.
runtime="$(az webapp config show -g "$RG" -n "$APP" --query linuxFxVersion -o tsv)"
case "$runtime" in
  PYTHON*) ;;
  *) echo "Web app runtime is '$runtime', expected PYTHON|3.12. Re-run the Bicep template (infra/appservice/main.bicep)." >&2; exit 1 ;;
esac
build_flag="$(az webapp config appsettings list -g "$RG" -n "$APP" --query "[?name=='SCM_DO_BUILD_DURING_DEPLOYMENT'].value | [0]" -o tsv)"
[ "$build_flag" = "true" ] || echo "WARNING: SCM_DO_BUILD_DURING_DEPLOYMENT is not 'true'; Python packages will not be installed. Re-run the Bicep template."

# ---------------------------------------------------------------- package
if [ -z "$PACKAGE" ]; then
  step "Build package"
  PACKAGE=".local/crip-app.zip"
  args=(--out "$PACKAGE"); [ "$SKIP_UI" -eq 1 ] && args+=(--skip-ui-build)
  "$PY" scripts/build_package.py "${args[@]}"
fi
[ -f "$PACKAGE" ] || { echo "Package not found: $PACKAGE" >&2; exit 1; }

# ---------------------------------------------------------------- deploy
step "Deploy $PACKAGE (App Service installs Python packages; this takes a few minutes)"
az webapp deploy -g "$RG" -n "$APP" --src-path "$PACKAGE" --type zip --restart true --timeout 1500000 -o none
echo "Deployment accepted."

# ---------------------------------------------------------------- smoke test
if [ "$SMOKE" -eq 1 ]; then
  step "Smoke test $URL"
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  ok=0
  for _ in $(seq 1 40); do
    if curl -fsS --max-time 10 "$URL/health/live" > /dev/null 2>&1; then ok=1; break; fi
    sleep 15
  done
  [ "$ok" -eq 1 ] || { echo "FAIL: $URL/health/live did not answer within 10 minutes. Check Log stream / 'az webapp log tail -g $RG -n $APP'." >&2; exit 1; }
  curl -fsS --max-time 10 "$URL/health" > "$tmp/health.json" || { echo "FAIL: $URL/health is not OK (store unreachable?)" >&2; exit 1; }
  echo "  PASS /health: $(cat "$tmp/health.json")"
  curl -fsS --max-time 10 "$URL/" | grep -q 'id="root"' || { echo "FAIL: UI not served at $URL/" >&2; exit 1; }
  echo "  PASS UI served"
  code="$(curl -sS -o "$tmp/chat.json" -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"message":"ping"}' "$URL/api/chat" || true)"
  if [ "$code" = 401 ] && grep -q '"unauthenticated"' "$tmp/chat.json"; then echo "  PASS API requires sign-in (401)"; else echo "FAIL: expected a 401 envelope from /api/chat, got $code" >&2; exit 1; fi
  demo="$(curl -fsS --max-time 10 "$URL/config.js" | grep -o '"demoMode": *[a-z]*' || true)"
  echo "  INFO $demo"
fi
echo
echo "CRIP is live at $URL"
