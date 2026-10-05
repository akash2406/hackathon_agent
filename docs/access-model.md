# Access model: who sees what

CRIP shows each person only what their access allows, **per subscription**, and enforces it on the
server for every dashboard call, chat question and admin endpoint. The UI hides what you can't use,
but it is never the gatekeeper.

## Levels

| Level | Can see | Granted by (any one is enough; the highest wins) |
|---|---|---|
| **Resources** | Overview (health), Security & reliability, Network & policy, Inventory & tags, Ask CRIP (posture questions) | Azure **Reader**, **Security Reader** or **Monitoring Reader** on the subscription (or a management group above it) · app role **CRIP.Reader** · a group in `CRIP_READER_GROUP_IDS` |
| **Cost** | Everything above **plus** Spend, Trends & forecast, Savings, cost questions | Azure **Owner**, **Contributor**, **Cost Management Reader/Contributor** or **Billing Reader** on the subscription (or above) · app role **CRIP.CostReader** · a group in `CRIP_COST_READER_GROUP_IDS` |
| **Platform admin** | Every subscription in scope at Cost level **plus** Estate overview, Access review, Usage log, Settings & health, and the Platform agent in chat | app role **CRIP.PlatformAdmin** · a group in `CRIP_PLATFORM_ADMIN_GROUP_IDS` |

A plain Azure **Reader** deliberately does **not** get cost views. Cost visibility is a deliberate
grant (a cost role or a CRIP role). Readers still get real value: security, network, policy and
inventory.

## How it is decided

1. **Sign-in.** The token carries the user's app roles (`roles`) and security groups (`groups`).
   If a user is in too many groups for the token, CRIP asks Microsoft Graph instead.
2. **Subscriptions in scope.** These are the subscriptions CRIP's identity can read, limited to
   `CRIP_MANAGEMENT_GROUP_ID` (and/or `CRIP_SUBSCRIPTION_IDS`).
3. **Azure RBAC check** (`CRIP_RBAC_ACCESS_CHECK=true`). For each subscription CRIP reads the user's
   role assignments, including those inherited via groups and from management groups, with
   `roleAssignments?$filter=assignedTo('<user>')`. **Resource-group-level grants don't count**, because
   the views are subscription-wide.
4. **Combine.** The highest grant per subscription wins. The result is cached for
   `CRIP_ACCESS_CACHE_SECONDS` (default 15 minutes).
5. **Enforce.** Every tool declares what it needs (resources / cost / admin). A refusal is an honest
   "Access denied: …" answer naming the role to ask for; no Azure call is made. Every source CRIP shows
   records **how** the user was allowed in (e.g. `rbac:Reader`, `app-role:CRIP.PlatformAdmin`), and
   every request, including denials, goes to the usage log.

## Two ways to read Azure (`CRIP_AZURE_ACCESS_MODE`)

| | `app_identity` (default) | `user_obo` |
|---|---|---|
| Who calls Azure | CRIP's managed identity (read-only roles on the management group) | The signed-in user (on-behalf-of token) |
| Setup | `scripts/grant-azure-access.sh` (Reader + Cost Management Reader + Security Reader on the MG, Graph Directory.Read.All) | Admin consent for Azure Service Management; federated credential or client secret |
| Granularity | Subscription-level, decided by CRIP as above | Exactly the user's RBAC, including resource-group level |
| Trade-off | Simple; the identity has standing read access to the estate | Most precise; more Entra setup |

The access levels apply in both modes. In `user_obo` mode Azure also enforces the user's own RBAC
on each call.

## Platform admin area

- **Estate overview:** every subscription's month-to-date cost, Defender secure score and Advisor
  counts. Cost needs `CRIP_MANAGEMENT_GROUP_ID` so it can be queried once at the management group.
- **Access review:** role assignments on a subscription (including inherited) with names from
  Microsoft Graph, plus findings: too many Owners, privileged roles granted directly to users, guests
  with privileged roles, and assignments to deleted identities. Names need Graph
  `Directory.Read.All`; without it the page says so and shows object IDs.
- **Usage log:** who used CRIP, what they did, and what was denied (CRIP's own `access_log` table).
  Users are told usage is logged (sidebar notice).
- **Settings & health:** access mode, management group, mappings, Graph status, agents, storage.

## Setting it up

```bash
# 1. CRIP's identity: read-only roles on the management group + Graph permission
bash scripts/grant-azure-access.sh --management-group <mg-id> --principal-id <managedIdentityPrincipalId>

# 2. App registration: app roles + group claims; optionally assign groups to roles
bash scripts/setup-entra-app.sh --name crip-team1 --url https://<web-app>.azurewebsites.net \
     --admin-group <platform-team-group-id> --cost-group <finops-group-id>

# 3. Web app settings (Bicep parameters): managementGroupId, and optional group id mappings
```

Users with no CRIP role and no group still get in if they hold an Azure role on a subscription in
scope (step 3 of "How it is decided").

## Known limits

- **PIM:** *eligible* roles that are not activated don't count (the user activates, then waits for the cache).
- **Custom roles** grant nothing in CRIP unless mapped. Deny assignments are not evaluated.
- **Group overage** needs Graph `Directory.Read.All` on CRIP's identity.
- **Cache:** changes take up to `CRIP_ACCESS_CACHE_SECONDS` to apply.
