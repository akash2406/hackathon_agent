/**
 * The UI's only door to the backend.
 *
 * ``Api`` has two implementations:
 *  - ``liveApi``: calls the backend with the signed-in user's token (MSAL).
 *    Every number it returns was computed by the backend from a real Azure call.
 *  - ``demoApi``: returns clearly labelled SAMPLE data from the browser bundle,
 *    with no sign-in, for UI previews only (config.demoMode). It never talks to Azure.
 *
 * Every non-2xx response is parsed as the backend's ErrorEnvelope and surfaced
 * as an ApiFailure, so the UI can show the honest error and correlation id.
 */
import {
  InteractionRequiredAuthError,
  type AccountInfo,
  type IPublicClientApplication,
} from "@azure/msal-browser";
import { apiTokenRequest, config } from "./config";
import type { AdminSettings, AgentResponse, Capabilities, ChatResponse, ErrorEnvelope, Me, UsageReport } from "./types";

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

export interface Api {
  readonly demo: boolean;
  me(): Promise<Me>;
  callTool(name: string, args: Record<string, unknown>): Promise<AgentResponse>;
  chat(message: string, sessionId: string | null): Promise<ChatResponse>;
  adminUsage(days: number): Promise<UsageReport>;
  adminSettings(): Promise<AdminSettings>;
}

export type DemoPersona = "admin" | "cost" | "reader";

async function accessToken(msal: IPublicClientApplication, account: AccountInfo): Promise<string> {
  try {
    return (await msal.acquireTokenSilent({ ...apiTokenRequest, account })).accessToken;
  } catch (err) {
    if (err instanceof InteractionRequiredAuthError) {
      return (await msal.acquireTokenPopup({ ...apiTokenRequest, account })).accessToken;
    }
    throw err;
  }
}

async function failureFrom(response: Response): Promise<ApiFailure> {
  let envelope: ErrorEnvelope | null = null;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    /* non-JSON error (e.g. a proxy) handled below */
  }
  return new ApiFailure(
    envelope?.error?.message ?? `Request failed with HTTP ${response.status}`,
    envelope?.error?.code ?? `http_${response.status}`,
    envelope?.error?.correlation_id ?? response.headers.get("x-correlation-id"),
    envelope?.error?.retryable ?? response.status >= 500,
  );
}

export function liveApi(msal: IPublicClientApplication, account: AccountInfo): Api {
  async function send<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    const token = await accessToken(msal, account);
    const response = await fetch(`${config.apiBaseUrl}${path}`, {
      method,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw await failureFrom(response);
    return (await response.json()) as T;
  }
  return {
    demo: false,
    me: () => send<Me>("GET", "/api/me"),
    callTool: (name, args) => send<AgentResponse>("POST", `/api/tools/${encodeURIComponent(name)}`, args),
    chat: (message, sessionId) => send<ChatResponse>("POST", "/api/chat", { message, session_id: sessionId }),
    adminUsage: (days) => send<UsageReport>("GET", `/api/admin/usage?days=${days}`),
    adminSettings: () => send<AdminSettings>("GET", "/api/admin/settings"),
  };
}

export function demoApi(persona: DemoPersona): Api {
  // Lazy import keeps the sample data out of the main bundle for real users.
  const load = () => import("./demo/sample");
  const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));
  return {
    demo: true,
    me: async () => (await load()).sampleMe(persona),
    callTool: async (name, args) => {
      await delay(350 + Math.random() * 400);
      return (await load()).sampleTool(name, args, persona);
    },
    chat: async (message) => {
      await delay(1600);
      return (await load()).sampleChat(message, persona);
    },
    adminUsage: async (days) => (await load()).sampleUsage(days),
    adminSettings: async () => (await load()).sampleSettings(),
  };
}

/** Public (no token): which agents exist and their example questions. */
export async function getCapabilities(): Promise<Capabilities> {
  const response = await fetch(`${config.apiBaseUrl}/api/capabilities`);
  if (!response.ok) return { agents: [] };
  return (await response.json()) as Capabilities;
}
