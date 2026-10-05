# Azure setup (App Service)

About 15 minutes for one team/environment. Everything is read-only for customer data; the only
"write" permissions are on the resources CRIP itself runs on.

## What you need

| Thing | Notes |
|---|---|
| A resource group | Contributor + permission to create role assignments (Owner, or Contributor + *Role Based Access Control Administrator*) for the person/pipeline deploying |
| An **Azure AI Foundry project** with a **model deployment that supports tool calling** (e.g. `gpt-4o`) | Note the project endpoint `https://<resource>.services.ai.azure.com/api/projects/<project>` and the deployment name |
| Permission to register an Entra app + grant admin consent | Or a tenant admin to run one command for you |
| Users with Azure RBAC on what they want to ask about | **Cost Management Reader** (or Reader) for spend/forecast; **Reader** for Advisor and Resource Graph. CRIP uses *their* access. |

## Step 1: Azure resources (Bicep)

```bash
az login
az group create -n rg-crip-team1 -l westeurope
# edit infra/appservice/main.bicepparam (namePrefix, Foundry endpoint/model); apiClientId can be a placeholder on the first run
az deployment group create -g rg-crip-team1 -n crip-appservice \
  -f infra/appservice/main.bicep -p infra/appservice/main.bicepparam \
  --query properties.outputs
```

Creates: **user-assigned managed identity**, **Log Analytics + Application Insights**, a **Linux App Service plan** (B1 default) and the
**web app** (HTTPS only, FTPS off, health check `/health`, persistent `/home` for the SQLite store).
Note the outputs `webAppUrl`, `webAppName`, `managedIdentityPrincipalId`.

## Step 2: Entra app registration (one registration = SPA + API)

```bash
bash scripts/setup-entra-app.sh --name crip-team1 \
  --url <webAppUrl> --mi-principal-id <managedIdentityPrincipalId>
```

The script (idempotent; review it before running):

- exposes the API scope `api://<appId>/access_as_user` (v2 tokens);
- adds SPA redirect URIs for the web app URL and localhost;
- creates the app roles **CRIP.PlatformAdmin**, **CRIP.CostReader**, **CRIP.Reader** and turns on
  security-group claims;
- only with `--obo` (per-user mode): adds Azure Service Management / user_impersonation with admin
  consent and a federated credential trusting the web app's managed identity.

Then re-run the Step 1 deployment with the real `apiClientId` (it only updates app settings).

> Portal equivalent: App registrations → New → *Expose an API* (scope `access_as_user`) →
> *Authentication* → add platform *Single-page application* with the URLs → *API permissions* → Azure
> Service Management → `user_impersonation` → Grant admin consent → *Certificates & secrets* →
> Federated credentials → scenario *Managed identity* → pick `<namePrefix>-id`.

## Step 2b: Give CRIP read access to the estate (default `app_identity` mode)

```bash
bash scripts/grant-azure-access.sh --management-group <mg-id> --principal-id <managedIdentityPrincipalId>
```

Grants the identity **Reader**, **Cost Management Reader** and **Security Reader** on the management
group, plus Microsoft Graph **Directory.Read.All** (names in the access review). It needs Owner/UAA on
the management group and a Privileged Role Administrator for the Graph step. Then set Bicep parameter
`managementGroupId` and, optionally, `platformAdminGroupIds` / `costReaderGroupIds` / `readerGroupIds`.
Assign people to the app roles (`CRIP.PlatformAdmin`, `CRIP.CostReader`, `CRIP.Reader`) in
Enterprise applications, or pass `--admin-group` / `--cost-group` to `setup-entra-app.sh`.
Who sees what: [access-model.md](access-model.md).

## Step 3: Let the app use Foundry

```bash
az role assignment create --assignee-object-id <managedIdentityPrincipalId> \
  --assignee-principal-type ServicePrincipal --role "Azure AI User" \
  --scope <resource id of the Foundry project (or its AI Services account)>
```

The app uses this identity to create/update its four agents on startup
(`CRIP_REGISTER_AGENTS_ON_STARTUP=true`) and to run them. It is **not** used to read any Azure cost
or resource data.

## Step 4: Deploy the code

Either the pipeline ([pipeline.md](pipeline.md)), or by hand (PowerShell, Git Bash, Linux or macOS):

```bash
python scripts/deploy.py --env dev            # add --provision to also run Step 1
```

It reads `.azuredevops/vars/common.yml` + `dev.yml`, then:

1. **settings**: sets the runtime, startup command and every `CRIP_*` app setting from the variable
   files (only the changed ones; it prints names, never values). The UI needs nothing separate: it
   reads its settings from `/config.js`, which the backend builds from these app settings;
2. **agents**: creates/updates the 7 agents in your Foundry project as *you* (`az login`; needs
   **Azure AI User** on the project). `--agents app` leaves it to the web app's managed identity at
   startup instead; demo mode skips it;
3. **code**: builds the UI, zips it with the API and agent definitions, `az webapp deploy`, and App
   Service installs `requirements.txt`;
4. **smoke test**: `/health`, the UI, the `/api/chat` 401, and that `/config.js` shows the new settings.

`--steps settings` only updates settings; `--set name=value` overrides a variable for one run.

Open `<webAppUrl>`, sign in, ask *"Give me a cost overview"*.

## App settings reference (set by Bicep)

| Setting | Meaning |
|---|---|
| `WEBSITES_PORT=8000` | the app listens on 8000 (startup command `python -m uvicorn --app-dir backend ...`) |
| `SCM_DO_BUILD_DURING_DEPLOYMENT=true` | App Service installs `requirements.txt` from the zip on each deployment |
| `CRIP_SQLITE_PATH=/home/data/crip.db` | SQLite on App Service persistent storage |
| `CRIP_UI_DEMO_MODE` | `true` only for a temporary sample-data preview (Bicep `uiDemoMode`) |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` | persistent `/home` (SQLite at `/home/data/crip.db`) |
| `AZURE_CLIENT_ID` | the user-assigned managed identity (Foundry + OBO client assertion) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | telemetry |
| `CRIP_TENANT_ID`, `CRIP_API_CLIENT_ID` (`CRIP_SPA_CLIENT_ID` optional) | Entra |
| `CRIP_OBO_CREDENTIAL_MODE` | `managed_identity` (default) or `client_secret` |
| `CRIP_SECRET_OBO_CLIENT_SECRET` | only for `client_secret`: a **Key Vault reference** `@Microsoft.KeyVault(SecretUri=...)` |
| `CRIP_SECRET_DATABASE_URL` | optional PostgreSQL DSN (Key Vault reference). Unset = SQLite |
| `CRIP_FOUNDRY_PROJECT_ENDPOINT`, `CRIP_FOUNDRY_MODEL_DEPLOYMENT`, `CRIP_REGISTER_AGENTS_ON_STARTUP` | Foundry |

Any secret is read by name: env var `CRIP_SECRET_<NAME>` (App Service Key Vault reference) or file
`.secrets/<name>` locally. No secret value is ever in code, the package, Bicep or pipeline files.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Site shows the default App Service page | No code deployed yet: run `python scripts/deploy.py --env <env>` |
| App fails to start, log shows `No module named ...` | Packages not installed: `SCM_DO_BUILD_DURING_DEPLOYMENT` must be `true` (Bicep sets it); redeploy |
| Chat returns 502 "Agent 'crip-orchestrator' is not registered" | Missing *Azure AI User* role on Foundry, or wrong model deployment. App logs (Log stream) show the registration error |
| Answers say "could not obtain a delegated Azure token (AADSTS65001)" | Admin consent for Azure Service Management not granted |
| "...(AADSTS70021 / 700213) federated credential..." | FIC subject must be the managed identity's **principal (object) id**; issuer `https://login.microsoftonline.com/<tenant>/v2.0` |
| Answers say "Azure denied access ... (403)" | Correct behaviour: the *user* lacks Reader/Cost Management Reader on that scope |
| Sign-in error "redirect URI mismatch" | Add the exact web app URL as an SPA redirect URI |
| Log: "SQLite directory ... is not writable" | Set `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`, or configure `CRIP_SECRET_DATABASE_URL` |
