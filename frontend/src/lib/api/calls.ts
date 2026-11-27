/**
 * API client for call history
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function getAuthHeaders(): HeadersInit {
  if (typeof window === "undefined") return {};
  const token = localStorage.getItem("access_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface CallRecord {
  id: string;
  provider: string;
  provider_call_id: string;
  agent_id: string | null;
  agent_name: string | null;
  contact_id: number | null;
  contact_name: string | null;
  workspace_id: string | null;
  workspace_name: string | null;
  direction: "inbound" | "outbound";
  status: string;
  from_number: string;
  to_number: string;
  duration_seconds: number;
  recording_url: string | null;
  transcript: string | null;
  started_at: string;
  answered_at: string | null;
  ended_at: string | null;
}

export interface CallRecordListResponse {
  calls: CallRecord[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ListCallsParams {
  page?: number;
  page_size?: number;
  agent_id?: string;
  workspace_id?: string;
  direction?: "inbound" | "outbound";
  status?: string;
  /** Matches agent name, contact name or either number, across every page. */
  search?: string;
}

export interface CallStats {
  total_calls: number;
  completed_calls: number;
  inbound_calls: number;
  outbound_calls: number;
  total_duration_seconds: number;
  average_duration_seconds: number;
}

/**
 * List call records with pagination and filtering
 */
export async function listCalls(params: ListCallsParams = {}): Promise<CallRecordListResponse> {
  const searchParams = new URLSearchParams();
  if (params.page) searchParams.set("page", params.page.toString());
  if (params.page_size) searchParams.set("page_size", params.page_size.toString());
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.workspace_id) searchParams.set("workspace_id", params.workspace_id);
  if (params.direction) searchParams.set("direction", params.direction);
  if (params.status) searchParams.set("status", params.status);
  if (params.search) searchParams.set("search", params.search);

  const response = await fetch(`${API_BASE}/api/v1/calls?${searchParams.toString()}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch calls");
  }

  return response.json();
}

/**
 * Get a specific call record
 */
export async function getCall(callId: string): Promise<CallRecord> {
  const response = await fetch(`${API_BASE}/api/v1/calls/${callId}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch call");
  }

  return response.json();
}

/**
 * Counts and average duration over every call the user has. Worked out by the
 * server, so it does not depend on how many calls a page happens to hold.
 */
export async function getCallStats(): Promise<CallStats> {
  const response = await fetch(`${API_BASE}/api/v1/calls/stats`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch call stats");
  }

  return response.json();
}

/**
 * Get call statistics for an agent
 */
export async function getAgentCallStats(agentId: string): Promise<CallStats> {
  const response = await fetch(`${API_BASE}/api/v1/calls/agent/${agentId}/stats`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch agent call stats");
  }

  return response.json();
}

/**
 * One measurement for one call. `value: null` means it could not be measured
 * for this call (no audio, or nothing to compare against); it does not mean zero.
 */
export interface CallMetricScore {
  metric: string;
  version: string;
  category: string;
  kind: string;
  value: number | null;
  passed: boolean | null;
  unit: string | null;
  detail: Record<string, unknown>;
}

export interface CallMetrics {
  call_id: string;
  outcome: string;
  trustworthy: boolean;
  has_audio: boolean;
  scores: CallMetricScore[];
  invalid_reasons: string[];
}

/**
 * Get what was measured on one real call
 */
export async function getCallMetrics(callId: string): Promise<CallMetrics> {
  const response = await fetch(`${API_BASE}/api/v1/calls/${callId}/metrics`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch call metrics");
  }

  return response.json();
}
