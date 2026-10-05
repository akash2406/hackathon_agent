#!/usr/bin/env bash
# Create or update the single Entra app registration CRIP uses (SPA + API), idempotently.
#
#   az login            # as someone who can register apps (and grant admin consent)
#   bash scripts/setup-entra-app.sh --name crip-team1 --url https://<web-app>.azurewebsites.net \
#        [--admin-group <objectId>] [--cost-group <objectId>] [--reader-group <objectId>] \
#        [--obo --mi-principal-id <id> [--local-secret]]
#
# Always configures:
#   * Expose an API: api://<appId>, delegated scope "access_as_user", v2 tokens
#   * SPA redirect URIs: --url, http://localhost:8000, http://localhost:5173
#   * App roles CRIP.PlatformAdmin, CRIP.CostReader, CRIP.Reader (docs/access-model.md)
#   * Security-group claims in tokens (for CRIP_*_GROUP_IDS mappings)
#   * --admin-group/--cost-group/--reader-group: assigns those groups to the app roles
#
# Only with --obo (CRIP_AZURE_ACCESS_MODE=user_obo, every Azure call as the user):
#   * Delegated permission Azure Service Management / user_impersonation + admin consent
#   * Federated credential trusting the web app's managed identity (--mi-principal-id)
#   * --local-secret: a 1-year client secret in .secrets/obo-client-secret (git-ignored)
#
# Needs: az CLI, python3. Safe to re-run (existing role IDs are kept).

set -euo pipefail

NAME=""; URL=""; MI=""; LOCAL_SECRET=0; OBO=0; ADMIN_GROUP=""; COST_GROUP=""; READER_GROUP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --url) URL="${2%/}"; shift 2 ;;
    --mi-principal-id) MI="$2"; shift 2 ;;
    --obo) OBO=1; shift ;;
    --local-secret) LOCAL_SECRET=1; shift ;;
    --admin-group) ADMIN_GROUP="$2"; shift 2 ;;
    --cost-group) COST_GROUP="$2"; shift 2 ;;
    --reader-group) READER_GROUP="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$NAME" ] || { echo "usage: $0 --name <display-name> [--url https://app] [--admin-group id] [--cost-group id] [--reader-group id] [--obo --mi-principal-id id [--local-secret]]" >&2; exit 2; }

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
EXISTING=$(az ad app show --id "$APP_ID" --query "{scopes: api.oauth2PermissionScopes, roles: appRoles}" -o json)

BODY=$(APP_ID="$APP_ID" URL="$URL" OBO="$OBO" EXISTING="$EXISTING" ARM="$ARM_APP_ID" IMP="$ARM_USER_IMPERSONATION" python3 - <<'PY'
import json, os, uuid
existing = json.loads(os.environ["EXISTING"] or "{}")
def keep(items, value):
    return next((i["id"] for i in (items or []) if i.get("value") == value), str(uuid.uuid4()))
scope_id = keep(existing.get("scopes"), "access_as_user")
roles = [
    ("CRIP.PlatformAdmin", "CRIP platform admin", "All subscriptions in scope plus the admin area (access review, estate, usage log)."),
    ("CRIP.CostReader", "CRIP cost reader", "Cost and resource views on every subscription in scope."),
    ("CRIP.Reader", "CRIP reader", "Resource, security and network views (no cost) on every subscription in scope."),
]
redirects = [u for u in [os.environ["URL"], "http://localhost:8000", "http://localhost:5173"] if u]
body = {
    "identifierUris": [f"api://{os.environ['APP_ID']}"],
    "groupMembershipClaims": "SecurityGroup",
    "api": {
        "requestedAccessTokenVersion": 2,
        "oauth2PermissionScopes": [{
            "id": scope_id, "value": "access_as_user", "type": "User", "isEnabled": True,
            "adminConsentDisplayName": "Use CRIP as the signed-in user",
            "adminConsentDescription": "Lets CRIP show Azure data the signed-in user is allowed to see. Read-only.",
            "userConsentDisplayName": "Use CRIP", "userConsentDescription": "Lets CRIP show Azure data you are allowed to see. Read-only.",
        }],
    },
    "appRoles": [
        {"id": keep(existing.get("roles"), v), "value": v, "displayName": d, "description": desc,
         "allowedMemberTypes": ["User"], "isEnabled": True}
        for v, d, desc in roles
    ],
    "spa": {"redirectUris": redirects},
    "requiredResourceAccess": (
        [{"resourceAppId": os.environ["ARM"], "resourceAccess": [{"id": os.environ["IMP"], "type": "Scope"}]}]
        if os.environ["OBO"] == "1" else []
    ),
}
print(json.dumps(body))
PY
)
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" --headers "Content-Type=application/json" --body "$BODY" > /dev/null
echo "configured: api://$APP_ID/access_as_user, app roles, group claims, SPA redirect URIs"

SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || az ad sp create --id "$APP_ID" --query id -o tsv)

assign() {  # group object id, app role value
  local group="$1" role="$2"
  [ -n "$group" ] || return 0
  local role_id
  role_id=$(az ad app show --id "$APP_ID" --query "appRoles[?value=='$role'].id | [0]" -o tsv)
  if az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
       --query "value[?principalId=='$group' && appRoleId=='$role_id'] | [0].id" -o tsv | grep -q .; then
    echo "group $group already has $role"
  else
    az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
      --headers "Content-Type=application/json" \
      --body "{\"principalId\": \"$group\", \"resourceId\": \"$SP_ID\", \"appRoleId\": \"$role_id\"}" > /dev/null
    echo "assigned $role to group $group"
  fi
}
assign "$ADMIN_GROUP" "CRIP.PlatformAdmin"
assign "$COST_GROUP" "CRIP.CostReader"
assign "$READER_GROUP" "CRIP.Reader"

if [ "$OBO" -eq 1 ]; then
  if az ad app permission admin-consent --id "$APP_ID" > /dev/null 2>&1; then
    echo "admin consent granted (Azure Service Management)"
  else
    echo "WARNING: ask a tenant admin to run: az ad app permission admin-consent --id $APP_ID"
  fi
  if [ -n "$MI" ] && ! az ad app federated-credential list --id "$APP_ID" --query "[?subject=='$MI'] | [0].name" -o tsv | grep -q .; then
    az ad app federated-credential create --id "$APP_ID" --parameters "{
      \"name\": \"crip-webapp-managed-identity\", \"issuer\": \"https://login.microsoftonline.com/$TENANT/v2.0\",
      \"subject\": \"$MI\", \"audiences\": [\"api://AzureADTokenExchange\"]}" > /dev/null
    echo "federated credential created for managed identity $MI"
  fi
  if [ "$LOCAL_SECRET" -eq 1 ]; then
    mkdir -p "$(dirname "$0")/../.secrets"
    az ad app credential reset --id "$APP_ID" --append --display-name "crip-local-dev" --years 1 --query password -o tsv \
      > "$(dirname "$0")/../.secrets/obo-client-secret"
    echo "local-dev client secret written to .secrets/obo-client-secret (git-ignored)"
  fi
fi

cat <<EOF

Done. Identifiers for your configuration (not secrets):
  CRIP_TENANT_ID=$TENANT
  CRIP_API_CLIENT_ID=$APP_ID
Assign users or groups to the app roles in Entra ID > Enterprise applications > $NAME > Users and groups.
EOF
