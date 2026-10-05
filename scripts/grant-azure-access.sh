#!/usr/bin/env bash
# Give CRIP's managed identity read-only access to the platform estate (app_identity mode).
#
#   az login            # needs Owner / User Access Administrator on the management group,
#                       # and Privileged Role Administrator (or Global Admin) for the Graph step
#   bash scripts/grant-azure-access.sh --management-group <mg-id> --principal-id <managedIdentityPrincipalId>
#
# Grants on the management group (inherited by every subscription under it):
#   * Reader                  inventory, network, policy, Advisor, role assignments (for RBAC checks)
#   * Cost Management Reader  spend, trends, forecast, idle-resource cost
#   * Security Reader         Defender for Cloud secure score and assessments
#   * CRIP Network Diagnostics  (custom role, created here) Network Doctor: Network Watcher IP flow
#                             verify / next hop and a NIC's effective NSG rules / routes. These are POST
#                             "actions" that evaluate configuration and change nothing; Reader lacks them.
# And, for names in the access review and group-overage lookups:
#   * Microsoft Graph application permission Directory.Read.All
#
# All read-only. Safe to re-run.

set -euo pipefail
MG=""; PRINCIPAL=""; SKIP_GRAPH=0; SKIP_NETDIAG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --management-group) MG="$2"; shift 2 ;;
    --principal-id) PRINCIPAL="$2"; shift 2 ;;
    --skip-graph) SKIP_GRAPH=1; shift ;;
    --skip-network-diagnostics) SKIP_NETDIAG=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$MG" ] && [ -n "$PRINCIPAL" ] || { echo "usage: $0 --management-group <id> --principal-id <managed identity principal id> [--skip-graph] [--skip-network-diagnostics]" >&2; exit 2; }

SCOPE="/providers/Microsoft.Management/managementGroups/$MG"
for role in "Reader" "Cost Management Reader" "Security Reader"; do
  if az role assignment list --assignee "$PRINCIPAL" --scope "$SCOPE" --role "$role" --query "[0].id" -o tsv | grep -q .; then
    echo "already has $role on $SCOPE"
  else
    az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
      --role "$role" --scope "$SCOPE" -o none
    echo "granted $role on $SCOPE"
  fi
done

if [ "$SKIP_NETDIAG" -eq 0 ]; then
  ROLE="CRIP Network Diagnostics"
  if az role definition list --name "$ROLE" --scope "$SCOPE" --custom-role-only true --query "[0].id" -o tsv | grep -q .; then
    echo "custom role '$ROLE' exists"
  else
    DEF="$(mktemp)"
    cat > "$DEF" <<JSON
{
  "Name": "$ROLE",
  "IsCustom": true,
  "Description": "Read-only network diagnostics for CRIP's Network Doctor: Network Watcher IP flow verify and next hop, effective NSG rules and routes. Changes nothing.",
  "Actions": [
    "Microsoft.Network/networkWatchers/read",
    "Microsoft.Network/networkWatchers/ipFlowVerify/action",
    "Microsoft.Network/networkWatchers/nextHop/action",
    "Microsoft.Network/networkInterfaces/effectiveNetworkSecurityGroups/action",
    "Microsoft.Network/networkInterfaces/effectiveRouteTable/action"
  ],
  "NotActions": [],
  "AssignableScopes": ["$SCOPE"]
}
JSON
    az role definition create --role-definition "@$DEF" -o none
    rm -f "$DEF"
    echo "created custom role '$ROLE'"
  fi
  if az role assignment list --assignee "$PRINCIPAL" --scope "$SCOPE" --role "$ROLE" --query "[0].id" -o tsv 2>/dev/null | grep -q .; then
    echo "already has $ROLE on $SCOPE"
  else
    # A new role definition takes a little while to replicate; retry the assignment.
    for attempt in 1 2 3 4 5 6; do
      if az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role "$ROLE" --scope "$SCOPE" -o none 2>/dev/null; then
        echo "granted $ROLE on $SCOPE"; break
      fi
      [ "$attempt" -eq 6 ] && { echo "could not assign $ROLE yet; re-run this script in a minute" >&2; exit 1; }
      sleep 15
    done
  fi
fi

if [ "$SKIP_GRAPH" -eq 0 ]; then
  GRAPH_SP=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv)
  DIRECTORY_READ_ALL="7ab1d382-f21e-4acd-a863-ba3e13f7da61"
  if az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$PRINCIPAL/appRoleAssignments" \
       --query "value[?appRoleId=='$DIRECTORY_READ_ALL'] | [0].id" -o tsv | grep -q .; then
    echo "already has Graph Directory.Read.All"
  else
    az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$PRINCIPAL/appRoleAssignments" \
      --headers "Content-Type=application/json" \
      --body "{\"principalId\": \"$PRINCIPAL\", \"resourceId\": \"$GRAPH_SP\", \"appRoleId\": \"$DIRECTORY_READ_ALL\"}" > /dev/null
    echo "granted Graph Directory.Read.All (names in the access review, group lookups)"
  fi
fi
echo "Done. Set CRIP_MANAGEMENT_GROUP_ID=$MG on the web app (Bicep parameter managementGroupId)."
