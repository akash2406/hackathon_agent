/**
 * Calls the backend chat API with the signed-in user's access token.
 *
 * Every non-2xx response is parsed as the backend's ErrorEnvelope and surfaced
 * as an ApiFailure, so the UI can show the honest error and correlation id
 * instead of guessing.
 */
import {
  InteractionRequiredAuthError,
  type AccountInfo,
  type IPublicClientApplication,
} from "@azure/msal-browser";
import { apiTokenRequest, config } from "./config";
import type { ChatResponse, ErrorEnvelope } from "./types";

export class ApiFailure extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly correlationId: string | null,
    readonly retryable: boolean,
  ) {
    super(message);
  }
}

async function accessToken(msal: IPublicClientApplication, account: AccountInfo): Promise<string> {
  try {
    const result = await msal.acquireTokenSilent({ ...apiTokenRequest, account });
    return result.accessToken;
  } catch (err) {
    if (err instanceof InteractionRequiredAuthError) {
      const result = await msal.acquireTokenPopup({ ...apiTokenRequest, account });
      return result.accessToken;
    }
    throw err;
  }
}

export async function postChat(
  msal: IPublicClientApplication,
  account: AccountInfo,
  message: string,
  sessionId: string | null,
): Promise<ChatResponse> {
  const token = await accessToken(msal, account);
  const response = await fetch(`${config.apiBaseUrl}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ message, session_id: sessionId }),
  });
  if (!response.ok) {
    let envelope: ErrorEnvelope | null = null;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      /* non-JSON error (e.g. proxy) handled below */
    }
    throw new ApiFailure(
      envelope?.error.message ?? `Request failed with HTTP ${response.status}`,
      envelope?.error.code ?? `http_${response.status}`,
      envelope?.error.correlation_id ?? response.headers.get("x-correlation-id"),
      envelope?.error.retryable ?? response.status >= 500,
    );
  }
  return (await response.json()) as ChatResponse;
}
