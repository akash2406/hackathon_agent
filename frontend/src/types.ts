/**
 * TypeScript mirror of backend/crip_backend/contracts.py. Keep the two in sync.
 */

export type ResponseStatus = "ok" | "partial" | "no_data" | "error";

export interface Confidence {
  level: "high" | "medium" | "low";
  score: number;
}

export interface Source {
  tool: string;
  api: string;
  invoked_at: string;
  scope: string;
  auth: "user_obo";
  request_id: string | null;
  http_status: number | null;
}

export interface AgentResponse {
  agent: string;
  status: ResponseStatus;
  answer: string;
  confidence: Confidence;
  data: Record<string, unknown> | null;
  query_used: string | null;
  data_timestamp: string | null;
  sources: Source[];
  caveats: string[];
}

export interface ChatResponse {
  session_id: string;
  message_id: string;
  answer: string;
  status: ResponseStatus;
  grounded: boolean;
  contributions: AgentResponse[];
  caveats: string[];
  created_at: string;
}

export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    correlation_id: string;
    retryable: boolean;
  };
}

export interface AgentCapability {
  key: string;
  name: string;
  description: string;
  summary: string;
  examples: string[];
}

export interface Capabilities {
  agents: AgentCapability[];
}

export type AccessLevel = "resources" | "cost";

export interface SubscriptionAccess {
  subscription_id: string;
  display_name: string;
  level: AccessLevel;
  via: string[];
}

export interface Me {
  user: { object_id: string; name: string | null; username: string | null };
  access_mode: string;
  is_platform_admin: boolean;
  global_level: string;
  global_via: string[];
  subscriptions: SubscriptionAccess[];
  resolved_at: string;
  warnings: string[];
}

export interface UsageReport {
  days: number;
  total_events: number;
  distinct_users: number;
  denied: number;
  top_users: { user: string; events: number }[];
  by_action: { name: string; events: number }[];
  by_scope: { name: string; events: number }[];
  events: { at: string; user_name: string | null; action: string; scope: string | null; outcome: string; latency_ms: number | null; detail: string | null }[];
}

export type AdminSettings = Record<string, unknown>;
