// Example parameters. Copy per team/environment and fill in. These are
// identifiers, not secrets: safe to commit.
using './main.bicep'

param namePrefix = 'crip-team1'
param apiClientId = '<app-registration-client-id>'
param foundryProjectEndpoint = 'https://<resource>.services.ai.azure.com/api/projects/<project>'
param foundryModelDeployment = 'gpt-4o'
param azureAccessMode = 'app_identity'
param managementGroupId = '<management-group-id>'
// Optional: Entra group object ids mapped to CRIP access levels (app roles work without these)
param platformAdminGroupIds = ''
param costReaderGroupIds = ''
param readerGroupIds = ''
param appServiceSku = 'B1'
// Temporary sample-data preview without an app registration. Set false once sign-in is configured.
param uiDemoMode = false
