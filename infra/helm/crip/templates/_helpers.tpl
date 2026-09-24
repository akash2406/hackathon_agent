{{/* Names and labels ------------------------------------------------------- */}}

{{- define "crip.fullname" -}}
{{- printf "%s-crip" .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}

{{- define "crip.labels" -}}
app.kubernetes.io/name: crip
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "crip.selector" -}}
app.kubernetes.io/name: crip
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "crip.image" -}}
{{- $a := .root.Values.allocations.acr -}}
{{- printf "%s/%s/%s:%s" $a.loginServer (trimSuffix "/" $a.repositoryPath) .component .root.Values.image.tag -}}
{{- end -}}

{{/* Allocation validation ---------------------------------------------------
Fails rendering with a message naming the missing landing-zone allocation.
Included from every workload template so no manifest renders without it. */}}
{{- define "crip.validateAllocations" -}}
{{- $a := .Values.allocations -}}
{{- $_ := required "Missing landing-zone allocation #1: allocations.namespace (AKS namespace)" $a.namespace -}}
{{- if ne .Release.Namespace $a.namespace -}}
{{- fail (printf "Allocation #1 mismatch: release namespace '%s' is not the allocated namespace '%s'" .Release.Namespace $a.namespace) -}}
{{- end -}}
{{- $_ := required "Missing landing-zone allocation #2: allocations.postgres.schema (PostgreSQL schema on the shared Flexible Server)" $a.postgres.schema -}}
{{- $_ := required "Missing landing-zone allocation #3: allocations.acr.loginServer (ACR)" $a.acr.loginServer -}}
{{- $_ := required "Missing landing-zone allocation #3: allocations.acr.repositoryPath (ACR repository path)" $a.acr.repositoryPath -}}
{{- $_ := required "Missing landing-zone allocation #4: allocations.workloadIdentity.clientId (user-assigned managed identity)" $a.workloadIdentity.clientId -}}
{{- $_ := required "Missing landing-zone allocation #5: allocations.keyVault.name (Key Vault)" $a.keyVault.name -}}
{{- $_ := required "Missing landing-zone allocation #5: allocations.keyVault.tenantId" $a.keyVault.tenantId -}}
{{- $_ := required "Missing landing-zone allocation #5: allocations.keyVault.postgresConnectionStringSecret (secret name)" $a.keyVault.postgresConnectionStringSecret -}}
{{- $_ := required "Missing landing-zone allocation #8: allocations.keyVault.appInsightsConnectionStringSecret (App Insights connection string secret name)" $a.keyVault.appInsightsConnectionStringSecret -}}
{{- $_ := required "Missing landing-zone allocation #6: allocations.entra.tenantId" $a.entra.tenantId -}}
{{- $_ := required "Missing landing-zone allocation #6: allocations.entra.apiClientId (backend API app registration with OBO)" $a.entra.apiClientId -}}
{{- $_ := required "Missing landing-zone allocation #6: allocations.entra.spaClientId (frontend SPA app registration)" $a.entra.spaClientId -}}
{{- $_ := required "Missing landing-zone allocation #7: allocations.foundry.projectEndpoint (Azure AI Foundry project)" $a.foundry.projectEndpoint -}}
{{- $_ := required "Missing landing-zone allocation #7: allocations.foundry.modelDeployment (model deployment in the Foundry project)" $a.foundry.modelDeployment -}}
{{- $_ := required "image.tag is required (set by the pipeline)" .Values.image.tag -}}
{{- if .Values.ingress.enabled -}}
{{- $_ := required "ingress.host is required when ingress.enabled" .Values.ingress.host -}}
{{- end -}}
{{- $_ := required "networkPolicy.ingressControllerNamespace is required (namespace of the landing zone's ingress controller)" .Values.networkPolicy.ingressControllerNamespace -}}
{{- end -}}

{{/* Hardened container security context shared by every container. */}}
{{- define "crip.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "crip.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Environment shared by the backend and the agent-registration Job.
Only non-secret values: secrets are files from the Key Vault CSI mount. */}}
{{- define "crip.backendEnv" -}}
{{- $a := .Values.allocations -}}
- name: CRIP_ENVIRONMENT
  value: {{ .Release.Namespace | quote }}
- name: CRIP_LOG_LEVEL
  value: {{ .Values.backend.logLevel | quote }}
- name: CRIP_TENANT_ID
  value: {{ $a.entra.tenantId | quote }}
- name: CRIP_API_CLIENT_ID
  value: {{ $a.entra.apiClientId | quote }}
{{- with $a.entra.apiAudience }}
- name: CRIP_API_AUDIENCE
  value: {{ . | quote }}
{{- end }}
- name: CRIP_OBO_CREDENTIAL_MODE
  value: {{ .Values.backend.oboCredentialMode | quote }}
- name: CRIP_SECRETS_DIR
  value: /mnt/secrets-store
- name: CRIP_POSTGRES_SECRET_NAME
  value: {{ $a.keyVault.postgresConnectionStringSecret | quote }}
- name: CRIP_APPINSIGHTS_SECRET_NAME
  value: {{ $a.keyVault.appInsightsConnectionStringSecret | quote }}
- name: CRIP_DB_SCHEMA
  value: {{ $a.postgres.schema | quote }}
- name: CRIP_FOUNDRY_PROJECT_ENDPOINT
  value: {{ $a.foundry.projectEndpoint | quote }}
- name: CRIP_FOUNDRY_MODEL_DEPLOYMENT
  value: {{ $a.foundry.modelDeployment | quote }}
{{- end -}}
