#!/usr/bin/env bash
# Create or update the single Entra app registration CRIP uses (SPA + API), idempotently.
#
#   az login            # as someone who can register apps (and grant admin consent)
#   bash scripts/setup-entra-app.sh --name crip-team1 \
#        --url https://<web-app>.azurewebsites.net \
#        --mi-principal-id <managedIdentityPrincipalId from the Bicep outputs> \
#        [--local-secret]
#
# What it configures:
#   * Expose an API: identifier api://<appId>, delegated scope "access_as_user", v2 tokens
#   * SPA platform redirect URIs: --url, http://localhost:8000, http://localhost:5173
#   * Delegated permission Azure Service Management / user_impersonation (needed for
#     on-behalf-of calls to Cost Management, Advisor, Resource Graph) + admin consent
#   * Federated identity credential trusting the web app's user-assigned managed
#     identity, so the OBO exchange in App Service needs NO client secret
#   * --local-secret: a 1-year client secret written to .secrets/obo-client-secret
#     (git-ignored) for running the backend on your laptop. Never commit it.
#
# Needs: az CLI, python3 (for JSON/UUID helpers). Safe to re-run.

set -euo pipefail

NAME=""; URL=""; MI=""; LOCAL_SECRET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --url) URL="${2%/}"; shift 2 ;;
    --mi-principal-id) MI="$2"; shift 2 ;;
    --local-secret) LOCAL_SECRET=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$NAME" ] || { echo "usage: $0 --name <display-name> [--url https://app] [--mi-principal-id <id>] [--local-secret]" >&2; exit 2; }

ARM_APP_ID="797f4846-ba00-4fd7-ba43-dac1f8f63013"          # Azure Service Management
ARM_USER_IMPERSONATION="41094075-9dad-400e-a0bd-54e686782033"
TENANT=$(az account show --query tenantId -o tsv)

APP_ID=$(az ad app list --display-name "$NAME" --query "[0].appId" -o tsv)
if [ -z "$APP_ID" ]; then
  APP_ID=$(az ad app create --display-name "$NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
  echo "created app registration $NAME ($APP_ID)"
else
  echo "updating existing app registration $NAME ($APP_ID)"
fi
OBJECT_ID=$(az ad app show --id "$APP_ID" --query id -o tsv)
SCOPE_ID=$(az ad app show --id "$APP_ID" --query "api.oauth2PermissionScopes[?value=='access_as_user'].id | [0]" -o tsv)
[ -n "$SCOPE_ID" ] || SCOPE_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')

BODY=$(APP_ID="$APP_ID" SCOPE_ID="$SCOPE_ID" URL="$URL" ARM="$ARM_APP_ID" IMP="$ARM_USER_IMPERSONATION" python3 - <<'PY'
import json, os
redirects = [u for u in [os.environ["URL"], "http://localhost:8000", "http://localhost:5173"] if u]
print(json.dumps({
    "identifierUris": [f"api://{os.environ['APP_ID']}"],
    "api": {
        "requestedAccessTokenVersion": 2,
        "oauth2PermissionScopes": [{
            "id": os.environ["SCOPE_ID"],
            "value": "access_as_user",
            "type": "User",
            "isEnabled": True,
            "adminConsentDisplayName": "Use CRIP as the signed-in user",
            "adminConsentDescription": "Lets CRIP read Azure cost, Advisor and inventory data with the signed-in user's own permissions.",
            "userConsentDisplayName": "Use CRIP as you",
            "userConsentDescription": "Lets CRIP read Azure data you already have access to, on your behalf. Read-only.",
        }],
    },
    "spa": {"redirectUris": redirects},
    "requiredResourceAccess": [
        {"resourceAppId": os.environ["ARM"], "resourceAccess": [{"id": os.environ["IMP"], "type": "Scope"}]}
    ],
}))
PY
)
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
  --headers "Content-Type=application/json" --body "$BODY" > /dev/null
echo "configured: api://$APP_ID/access_as_user, SPA redirect URIs, Azure Service Management delegated permission"

az ad sp show --id "$APP_ID" > /dev/null 2>&1 || az ad sp create --id "$APP_ID" > /dev/null
if az ad app permission admin-consent --id "$APP_ID" > /dev/null 2>&1; then
  echo "admin consent granted"
else
  echo "WARNING: could not grant admin consent (needs a privileged role). Ask a tenant admin to run:"
  echo "         az ad app permission admin-consent --id $APP_ID"
fi

if [ -n "$MI" ]; then
  if az ad app federated-credential list --id "$APP_ID" --query "[?subject=='$MI'] | [0].name" -o tsv | grep -q .; then
    echo "federated credential for managed identity $MI already present"
  else
    az ad app federated-credential create --id "$APP_ID" --parameters "{
      \"name\": \"crip-webapp-managed-identity\",
      \"issuer\": \"https://login.microsoftonline.com/$TENANT/v2.0\",
      \"subject\": \"$MI\",
      \"audiences\": [\"api://AzureADTokenExchange\"],
      \"description\": \"CRIP web app: OBO exchange without a client secret\"
    }" > /dev/null
    echo "federated credential created: app trusts managed identity $MI (CRIP_OBO_CREDENTIAL_MODE=managed_identity)"
  fi
fi

if [ "$LOCAL_SECRET" -eq 1 ]; then
  mkdir -p "$(dirname "$0")/../.secrets"
  az ad app credential reset --id "$APP_ID" --append --display-name "crip-local-dev" --years 1 --query password -o tsv \
    > "$(dirname "$0")/../.secrets/obo-client-secret"
  echo "local-dev client secret written to .secrets/obo-client-secret (git-ignored; expires in 1 year)"
fi

cat <<EOF

Done. Values for your configuration (identifiers, not secrets):
  CRIP_TENANT_ID=$TENANT
  CRIP_API_CLIENT_ID=$APP_ID
  apiClientId (pipeline vars / Bicep) = $APP_ID
EOF
