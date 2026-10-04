// Example parameters. Copy per team/environment and fill in. These are
// identifiers, not secrets: safe to commit.
using './main.bicep'

param namePrefix = 'crip-team1'
param apiClientId = '<app-registration-client-id>'
param foundryProjectEndpoint = 'https://<resource>.services.ai.azure.com/api/projects/<project>'
param foundryModelDeployment = 'gpt-4o'
param oboCredentialMode = 'managed_identity'
param appServiceSku = 'B1'
