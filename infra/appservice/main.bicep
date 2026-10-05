// CRIP on Azure App Service (Linux, Python 3.12, code deployment): everything except Entra and Foundry.
//
//   az deployment group create -g <rg> -f infra/appservice/main.bicep -p infra/appservice/main.bicepparam
//   bash scripts/deploy-appservice.sh -g <rg>          # then deploy the code (zip)
//
// Creates: user-assigned managed identity, Log Analytics + Application Insights,
// Linux App Service plan and the web app (Python runtime; App Service installs the
// packages from the uploaded zip). Re-running is idempotent.
//
// Not created here (see docs/azure-setup.md):
//   * the Entra app registration (scripts/setup-entra-app.sh), and its federated
//     credential trusting the managed identity output below;
//   * the Azure AI Foundry project + model deployment, and the "Azure AI User"
//     role for the managed identity on it;
//   * read-only roles for the managed identity on the management group and the
//     Graph Directory.Read.All permission (scripts/grant-azure-access.sh).

@description('Short prefix for resource names, e.g. "crip-team1". Lowercase letters, digits and hyphens.')
@minLength(3)
@maxLength(20)
param namePrefix string

param location string = resourceGroup().location

@description('App Service plan SKU. B1 is enough for a demo; P0v3/P1v3 for more headroom.')
param appServiceSku string = 'B1'

param tenantId string = subscription().tenantId

@description('Application (client) ID of the CRIP app registration (API + SPA).')
param apiClientId string

@description('Only if the SPA uses a separate app registration. Leave empty for the recommended single registration.')
param spaClientId string = ''

@description('Azure AI Foundry project endpoint: https://<resource>.services.ai.azure.com/api/projects/<project>')
param foundryProjectEndpoint string

@description('Model deployment name in the Foundry project (must support tool calling), e.g. gpt-4o.')
param foundryModelDeployment string

@description('app_identity (default): CRIP managed identity reads Azure and CRIP checks the access of each user. user_obo: every call runs as the user.')
@allowed(['app_identity', 'user_obo'])
param azureAccessMode string = 'app_identity'

@description('Management group CRIP covers (grant the identity Reader, Cost Management Reader, Security Reader on it: scripts/grant-azure-access.sh).')
param managementGroupId string = ''

@description('Entra security group object ids (comma-separated) mapped to CRIP access levels. App roles work without these.')
param platformAdminGroupIds string = ''
param costReaderGroupIds string = ''
param readerGroupIds string = ''

@description('user_obo mode only: how the app proves its identity for the OBO exchange. managed_identity = no secret (recommended).')
@allowed(['managed_identity', 'client_secret'])
param oboCredentialMode string = 'managed_identity'

@description('client_secret mode only: Key Vault secret URI of the app registration client secret (the identity needs Key Vault Secrets User).')
// A URI pointing at the secret, not the secret itself; App Service resolves it at runtime.
#disable-next-line secure-secrets-in-params
param oboClientSecretKeyVaultUri string = ''

@description('Create/update the Foundry agents when the app starts.')
param registerAgentsOnStartup bool = true

@description('TEMPORARY preview only: serve the UI with labelled SAMPLE data and no sign-in (no app registration needed). The API still requires sign-in and returns no Azure data. Keep false for real use.')
param uiDemoMode bool = false

var suffix = uniqueString(resourceGroup().id, namePrefix)
// Code deployment: scripts/deploy-appservice.sh uploads a zip (API + built UI) and App Service's
// build (Oryx) installs requirements.txt. The zip keeps the repo layout, hence --app-dir backend.
var startupCommand = 'python -m uvicorn --app-dir backend --factory crip_backend.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id'
  location: location
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-ai'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${namePrefix}-plan'
  location: location
  kind: 'linux'
  sku: { name: appServiceSku }
  properties: { reserved: true }
}

var baseSettings = [
  { name: 'WEBSITES_PORT', value: '8000' }
  // Install requirements.txt from the uploaded zip (Oryx build) on every deployment.
  { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
  // Trust X-Forwarded-* from App Service's front end (read by uvicorn; an env var avoids shell globbing of '*').
  { name: 'FORWARDED_ALLOW_IPS', value: '*' }
  // /home is persistent on App Service: the SQLite store survives restarts and redeployments.
  { name: 'CRIP_SQLITE_PATH', value: '/home/data/crip.db' }
  // DefaultAzureCredential (Foundry) and the OBO federated credential use this identity.
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
  { name: 'CRIP_ENVIRONMENT', value: namePrefix }
  { name: 'CRIP_TENANT_ID', value: tenantId }
  { name: 'CRIP_API_CLIENT_ID', value: apiClientId }
  { name: 'CRIP_AZURE_ACCESS_MODE', value: azureAccessMode }
  { name: 'CRIP_MANAGEMENT_GROUP_ID', value: managementGroupId }
  { name: 'CRIP_PLATFORM_ADMIN_GROUP_IDS', value: platformAdminGroupIds }
  { name: 'CRIP_COST_READER_GROUP_IDS', value: costReaderGroupIds }
  { name: 'CRIP_READER_GROUP_IDS', value: readerGroupIds }
  { name: 'CRIP_OBO_CREDENTIAL_MODE', value: oboCredentialMode }
  { name: 'CRIP_FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
  { name: 'CRIP_FOUNDRY_MODEL_DEPLOYMENT', value: foundryModelDeployment }
  { name: 'CRIP_REGISTER_AGENTS_ON_STARTUP', value: string(registerAgentsOnStartup) }
  // Sample-data UI preview (temporary, before the app registration exists). Off by default.
  { name: 'CRIP_UI_DEMO_MODE', value: string(uiDemoMode) }
]
var spaSettings = empty(spaClientId) ? [] : [{ name: 'CRIP_SPA_CLIENT_ID', value: spaClientId }]
// A Key Vault reference: the secret value never appears in settings or code.
var secretSettings = empty(oboClientSecretKeyVaultUri)
  ? []
  : [{ name: 'CRIP_SECRET_OBO_CLIENT_SECRET', value: '@Microsoft.KeyVault(SecretUri=${oboClientSecretKeyVaultUri})' }]

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: '${namePrefix}-${take(suffix, 6)}'
  location: location
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    keyVaultReferenceIdentity: identity.id
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: startupCommand
      alwaysOn: true
      healthCheckPath: '/health'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      appSettings: concat(baseSettings, spaSettings, secretSettings)
    }
  }
}

output webAppName string = site.name
output webAppUrl string = 'https://${site.properties.defaultHostName}'
output managedIdentityClientId string = identity.properties.clientId
output managedIdentityPrincipalId string = identity.properties.principalId
output managedIdentityResourceId string = identity.id
