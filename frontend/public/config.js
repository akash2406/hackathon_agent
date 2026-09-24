// Local-development runtime config. In AKS this file is replaced by the Helm
// ConfigMap (infra/helm/crip/templates/frontend-configmap.yaml).
// These are public identifiers, not secrets: an SPA's client ID and tenant ID
// are visible to every browser that loads the app.
window.CRIP_CONFIG = {
  tenantId: "<your-tenant-id>",
  spaClientId: "<frontend-spa-app-registration-client-id>",
  apiScope: "api://<backend-api-app-registration-client-id>/access_as_user",
  apiBaseUrl: "",
};
