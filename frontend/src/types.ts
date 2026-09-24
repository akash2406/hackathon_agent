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
