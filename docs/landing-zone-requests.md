# CRIP: landing-zone allocation requests

**Application:** Cloud Resource Intelligence Platform (CRIP), hackathon build
**Deployment model:** Flow 2 tenant application into the DaaS multi-tenant AKS landing zone
**Provisions Azure infrastructure itself:** No. The Helm chart only consumes the allocations below
and fails to render, naming the missing item, if any is absent.

## Allocations

| # | Allocation | Details we need | Used for | Helm value(s) |
|---|---|---|---|---|
| 1 | **AKS namespace** | One namespace sized for 2× backend (0.1–0.5 vCPU, 256–512 Mi each) + 2× nginx frontend (≤0.2 vCPU, ≤128 Mi each) + a short-lived registration Job. No Redis, no extra agent workloads. An ingress host on the landing-zone ingress controller, and that controller's namespace name. A Kubernetes service connection for the pipeline scoped to this namespace. | Deployment target | `allocations.namespace`, `ingress.host`, `ingress.className`, `networkPolicy.ingressControllerNamespace` |
| 2 | **PostgreSQL schema** on the existing shared Flexible Server | Schema name, plus a login that owns (or can create tables in) that schema only. | Sessions, messages, agent-invocation audit trail | `allocations.postgres.schema` |
| 3 | **ACR repository path** | Registry login server + repository path prefix (we push `<path>/backend` and `<path>/frontend`). Push rights for the pipeline's Docker service connection; pull rights for the AKS kubelet identity. | Image storage | `allocations.acr.loginServer`, `allocations.acr.repositoryPath` |
| 4 | **User-assigned managed identity** with a federated credential | Federated credential: issuer = the cluster's OIDC issuer URL, subject = `system:serviceaccount:<namespace>:crip`, audience `api://AzureADTokenExchange`. We need the identity's **client ID**. | Workload Identity (Foundry calls, Key Vault CSI, agent registration) | `allocations.workloadIdentity.clientId` |
| 5 | **Key Vault scope** | `get`/`list` on secrets for the identity in #4. Two secrets (names to be confirmed by you): **PostgreSQL connection string** (libpq URI with `sslmode=require`, for the login in #2), **Application Insights connection string** (#8). | DB connection, telemetry | `allocations.keyVault.name`, `.tenantId`, `.postgresConnectionStringSecret`, `.appInsightsConnectionStringSecret` |
| 6 | **Two Entra app registrations** | **(a) Backend API:** exposes scope `access_as_user`; `accessTokenAcceptedVersion: 2`; delegated API permission on **Azure Service Management → `user_impersonation`** with admin consent (this is what makes **on-behalf-of** work); a **federated identity credential** with the same issuer/subject as #4 (so the OBO exchange needs no client secret); the frontend SPA listed as a known/pre-authorised client. **(b) Frontend SPA:** platform "Single-page application" with the ingress URL (and `http://localhost:5173` for dev) as redirect URIs; delegated permission to the backend's `access_as_user`. We need the tenant ID and both client IDs. **OBO must be configured before we build on it; there is no workaround if it is missing.** | Sign-in and the OBO token exchange | `allocations.entra.tenantId`, `.apiClientId`, `.spaClientId` (optional `.apiAudience` if not `api://<apiClientId>`) |
| 7 | **Azure AI Foundry project** | Project endpoint (`https://<resource>.services.ai.azure.com/api/projects/<project>`), a chat model deployment that supports tool calling (e.g. gpt-4o) and its **deployment name**, and a role for the identity in #4 permitting agent create/update and thread/run management (e.g. *Azure AI User* on the project). | Hosting the Orchestrator and CostPulse agents | `allocations.foundry.projectEndpoint`, `.modelDeployment` |
| 8 | **Application Insights connection string** | Stored as a secret in the Key Vault from #5. | Telemetry | `allocations.keyVault.appInsightsConnectionStringSecret` |

Users need no new role assignments from the landing zone. Each user's existing Azure RBAC (e.g.
**Cost Management Reader** or **Reader** on their subscriptions) decides what CRIP can show them,
because every Cost Management call runs with that user's own delegated token. The platform identity
(#4) must **not** be granted access to customer subscriptions.

## Egress firewall rules (upstream, FQDN-based)

NetworkPolicy in the chart allows backend egress on TCP 443/5432 only. FQDN filtering must be done by
the landing-zone firewall:

| Destination | Port | Why |
|---|---|---|
| `management.azure.com` | 443 | Cost Management Query API, subscription list (user's OBO token) |
| `login.microsoftonline.com` | 443 | Token-signing keys, OBO exchange, Workload Identity token exchange |
| `*.services.ai.azure.com` | 443 | Azure AI Foundry Agent Service |
| `*.openai.azure.com` | 443 | Model inference behind Foundry |
| `*.vault.azure.net` | 443 | Key Vault (CSI driver) |
| `*.azurecr.io` | 443 | Image pulls (kubelet) |
| `*.postgres.database.azure.com` | 5432 | PostgreSQL Flexible Server |
| `*.in.applicationinsights.azure.com` | 443 | Telemetry ingestion |

## CI/CD: self-hosted Azure DevOps agents

Deployment runs from a self-hosted agent pool (details: [pipeline.md](pipeline.md)). We need:

- A Linux agent pool with Python 3.12, Node 22, Docker, Helm 3, kubectl (+ kubelogin if the cluster
  uses Entra ID auth).
- Agent egress to: Azure DevOps, the ACR from #3, the AKS API server (private endpoint if applicable),
  `login.microsoftonline.com`, the internal Nexus PyPI repository (all Python packages), npm and
  Docker Hub **or** internal mirrors, and the CRIP ingress host
  (post-deploy smoke test).
- A Kubernetes service connection scoped to namespace #1, and a Docker Registry service connection
  with push rights to the repository path in #3.

## What we will hand back to you

Nothing is created outside the namespace. Inside it: 2 Deployments, 2 Services, 1 Ingress, 1
ConfigMap, 1 ServiceAccount, 1 SecretProviderClass, 5 NetworkPolicies, and a post-install Job.
