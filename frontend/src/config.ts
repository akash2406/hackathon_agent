/**
 * Runtime configuration and MSAL setup.
 *
 * Flow position: step 1 of "a user asking a question". The user signs in with
 * the frontend SPA app registration and we request an access token whose
 * audience is the *backend API* (apiScope). The backend later exchanges that
 * token on-behalf-of the user for an Azure Resource Manager token.
 */
import type { Configuration, PopupRequest } from "@azure/msal-browser";

export interface CripConfig {
  tenantId: string;
  spaClientId: string;
  apiScope: string;
  apiBaseUrl: string;
  /** Sample-data preview without sign-in. Set by the server (CRIP_UI_DEMO_MODE); never on in Azure. */
  demoMode: boolean;
}

declare global {
  interface Window {
    CRIP_CONFIG?: Partial<CripConfig>;
  }
}

function loadConfig(): CripConfig {
  const raw = window.CRIP_CONFIG ?? {};
  const missing = (["tenantId", "spaClientId", "apiScope"] as const).filter(
    (k) => !raw[k] || String(raw[k]).startsWith("<"),
  );
  if (missing.length > 0) {
    throw new Error(`config.js is missing: ${missing.join(", ")}. See README "Run it".`);
  }
  return { apiBaseUrl: "", demoMode: false, ...raw } as CripConfig;
}

export const config = loadConfig();

export const msalConfig: Configuration = {
  auth: {
    clientId: config.spaClientId,
    authority: `https://login.microsoftonline.com/${config.tenantId}`,
    redirectUri: window.location.origin,
    postLogoutRedirectUri: window.location.origin,
  },
  // sessionStorage: tokens do not outlive the browser tab.
  cache: { cacheLocation: "sessionStorage" },
};

export const apiTokenRequest: PopupRequest = { scopes: [config.apiScope] };
