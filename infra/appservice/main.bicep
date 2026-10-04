// CRIP on Azure App Service (Web App for Containers): everything except Entra and Foundry.
//
//   az deployment group create -g <rg> -f infra/appservice/main.bicep -p infra/appservice/main.bicepparam
//
// Creates: user-assigned managed identity, Azure Container Registry (+ AcrPull for
// the identity), Log Analytics + Application Insights, Linux App Service plan and
// the web app running the CRIP container. Re-running is idempotent.
//
// Not created here (see docs/azure-setup.md):
//   * the Entra app registration (scripts/setup-entra-app.sh), and its federated
//     credential trusting the managed identity output below;
//   * the Azure AI Foundry project + model deployment, and the "Azure AI User"
//     role for the managed identity on it.

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

@description('How the app proves its identity for the OBO exchange. managed_identity = no secret (recommended).')
@allowed(['managed_identity', 'client_secret'])
param oboCredentialMode string = 'managed_identity'

@description('client_secret mode only: Key Vault secret URI of the app registration client secret (the identity needs Key Vault Secrets User).')
// A URI pointing at the secret, not the secret itself; App Service resolves it at runtime.
#disable-next-line secure-secrets-in-params
param oboClientSecretKeyVaultUri string = ''

@description('Container image to run. Empty = <acr>/crip:latest (pushed by the pipeline).')
param containerImage string = ''

@description('Create/update the Foundry agents when the app starts.')
param registerAgentsOnStartup bool = true

var suffix = uniqueString(resourceGroup().id, namePrefix)
var acrName = take(toLower(replace('${namePrefix}${suffix}', '-', '')), 50)
var image = empty(containerImage) ? '${acr.properties.loginServer}/crip:latest' : containerImage

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id'
  location: location
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  // namePrefix (>= 3 chars) + a 13-char uniqueString: always >= 16 characters.
  #disable-next-line BCP334
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// AcrPull so App Service pulls the image with the managed identity (no registry password).
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, 'acrpull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
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
  // Persistent /home: the SQLite store (CRIP_SQLITE_PATH=/home/data/crip.db) survives restarts.
  { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'true' }
  // DefaultAzureCredential (Foundry) and the OBO federated credential use this identity.
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
  { name: 'CRIP_ENVIRONMENT', value: namePrefix }
  { name: 'CRIP_TENANT_ID', value: tenantId }
  { name: 'CRIP_API_CLIENT_ID', value: apiClientId }
  { name: 'CRIP_OBO_CREDENTIAL_MODE', value: oboCredentialMode }
  { name: 'CRIP_FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
  { name: 'CRIP_FOUNDRY_MODEL_DEPLOYMENT', value: foundryModelDeployment }
  { name: 'CRIP_REGISTER_AGENTS_ON_STARTUP', value: string(registerAgentsOnStartup) }
  // Sample-data UI preview: never in Azure.
  { name: 'CRIP_UI_DEMO_MODE', value: 'false' }
]
var spaSettings = empty(spaClientId) ? [] : [{ name: 'CRIP_SPA_CLIENT_ID', value: spaClientId }]
// A Key Vault reference: the secret value never appears in settings or code.
var secretSettings = empty(oboClientSecretKeyVaultUri)
  ? []
  : [{ name: 'CRIP_SECRET_OBO_CLIENT_SECRET', value: '@Microsoft.KeyVault(SecretUri=${oboClientSecretKeyVaultUri})' }]

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: '${namePrefix}-${take(suffix, 6)}'
  location: location
  kind: 'app,linux,container'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    keyVaultReferenceIdentity: identity.id
    siteConfig: {
      linuxFxVersion: 'DOCKER|${image}'
      acrUseManagedIdentityCreds: true
      acrUserManagedIdentityID: identity.properties.clientId
      alwaysOn: true
      healthCheckPath: '/health'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      appSettings: concat(baseSettings, spaSettings, secretSettings)
    }
  }
  dependsOn: [acrPull]
}

output webAppName string = site.name
output webAppUrl string = 'https://${site.properties.defaultHostName}'
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output managedIdentityClientId string = identity.properties.clientId
output managedIdentityPrincipalId string = identity.properties.principalId
output managedIdentityResourceId string = identity.id
