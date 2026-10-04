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

Creates: **user-assigned managed identity**, **ACR** (Basic, no admin user) with **AcrPull** for the
identity, **Log Analytics + Application Insights**, a **Linux App Service plan** (B1 default) and the
**web app** (HTTPS only, FTPS off, health check `/health`, persistent `/home` for the SQLite store).
Note the outputs `webAppUrl`, `acrName`, `managedIdentityPrincipalId`.

## Step 2: Entra app registration (one registration = SPA + API)

```bash
bash scripts/setup-entra-app.sh --name crip-team1 \
  --url <webAppUrl> --mi-principal-id <managedIdentityPrincipalId>
```

The script (idempotent; review it before running):

- exposes the API scope `api://<appId>/access_as_user` (v2 tokens);
- adds SPA redirect URIs for the web app URL and localhost;
- adds the delegated permission **Azure Service Management / user_impersonation** and grants admin
  consent. This is what lets the app call Azure **on behalf of** the user;
- adds a **federated identity credential trusting the web app's managed identity**, so the OBO
  exchange needs **no client secret** (`CRIP_OBO_CREDENTIAL_MODE=managed_identity`).

Then re-run the Step 1 deployment with the real `apiClientId` (it only updates app settings).

> Portal equivalent: App registrations → New → *Expose an API* (scope `access_as_user`) →
> *Authentication* → add platform *Single-page application* with the URLs → *API permissions* → Azure
> Service Management → `user_impersonation` → Grant admin consent → *Certificates & secrets* →
> Federated credentials → scenario *Managed identity* → pick `<namePrefix>-id`.

## Step 3: Let the app use Foundry

```bash
az role assignment create --assignee-object-id <managedIdentityPrincipalId> \
  --assignee-principal-type ServicePrincipal --role "Azure AI User" \
  --scope <resource id of the Foundry project (or its AI Services account)>
```

The app uses this identity to create/update its four agents on startup
(`CRIP_REGISTER_AGENTS_ON_STARTUP=true`) and to run them. It is **not** used to read any Azure cost
or resource data.

## Step 4: Build and deploy the image

Either the pipeline ([pipeline.md](pipeline.md)), or by hand:

```bash
az acr build -r <acrName> -t crip:latest .          # builds in Azure, no local Docker needed
# or: docker build -t <acrName>.azurecr.io/crip:latest . && az acr login -n <acrName> && docker push <acrName>.azurecr.io/crip:latest
az webapp restart -g rg-crip-team1 -n <webAppName>
```

Open `<webAppUrl>`, sign in, ask *"Give me a cost overview"*.

## App settings reference (set by Bicep)

| Setting | Meaning |
|---|---|
| `WEBSITES_PORT=8000` | the container listens on 8000 |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` | persistent `/home` (SQLite at `/home/data/crip.db`) |
| `AZURE_CLIENT_ID` | the user-assigned managed identity (Foundry + OBO client assertion) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | telemetry |
| `CRIP_TENANT_ID`, `CRIP_API_CLIENT_ID` (`CRIP_SPA_CLIENT_ID` optional) | Entra |
| `CRIP_OBO_CREDENTIAL_MODE` | `managed_identity` (default) or `client_secret` |
| `CRIP_SECRET_OBO_CLIENT_SECRET` | only for `client_secret`: a **Key Vault reference** `@Microsoft.KeyVault(SecretUri=...)` |
| `CRIP_SECRET_DATABASE_URL` | optional PostgreSQL DSN (Key Vault reference). Unset = SQLite |
| `CRIP_FOUNDRY_PROJECT_ENDPOINT`, `CRIP_FOUNDRY_MODEL_DEPLOYMENT`, `CRIP_REGISTER_AGENTS_ON_STARTUP` | Foundry |

Any secret is read by name: env var `CRIP_SECRET_<NAME>` (App Service Key Vault reference) or file
`.secrets/<name>` locally. No secret value is ever in code, the image, Bicep or pipeline files.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Container keeps restarting, logs show image pull errors | Image not pushed yet, or AcrPull missing. Check `az acr repository list -n <acr>` |
| Chat returns 502 "Agent 'crip-orchestrator' is not registered" | Missing *Azure AI User* role on Foundry, or wrong model deployment. App logs (Log stream) show the registration error |
| Answers say "could not obtain a delegated Azure token (AADSTS65001)" | Admin consent for Azure Service Management not granted |
| "...(AADSTS70021 / 700213) federated credential..." | FIC subject must be the managed identity's **principal (object) id**; issuer `https://login.microsoftonline.com/<tenant>/v2.0` |
| Answers say "Azure denied access ... (403)" | Correct behaviour: the *user* lacks Reader/Cost Management Reader on that scope |
| Sign-in error "redirect URI mismatch" | Add the exact web app URL as an SPA redirect URI |
| Log: "SQLite directory ... is not writable" | Set `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`, or configure `CRIP_SECRET_DATABASE_URL` |
