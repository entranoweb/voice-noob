/**
 * API client for QA evaluation and testing
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function getAuthHeaders(): HeadersInit {
  if (typeof window === "undefined") return {};
  const token = localStorage.getItem("access_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// =============================================================================
// Types
// =============================================================================

export interface CallEvaluation {
  id: string;
  call_id: string;
  agent_id: string | null;
  workspace_id: string | null;
  overall_score: number;
  intent_completion: number | null;
  tool_usage: number | null;
  compliance: number | null;
  response_quality: number | null;
  passed: boolean;
  coherence: number | null;
  relevance: number | null;
  groundedness: number | null;
  fluency: number | null;
  overall_sentiment: string | null;
  sentiment_score: number | null;
  escalation_risk: number | null;
  objectives_detected: string[] | null;
  objectives_completed: string[] | null;
  failure_reasons: string[] | null;
  recommendations: string[] | null;
  evaluation_model: string;
  evaluation_latency_ms: number | null;
  evaluation_cost_cents: number | null;
  created_at: string;
}

export interface CallEvaluationListResponse {
  evaluations: CallEvaluation[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ListEvaluationsParams {
  page?: number;
  page_size?: number;
  agent_id?: string;
  workspace_id?: string;
  passed?: boolean;
}

export interface QAStatus {
  enabled: boolean;
  auto_evaluate: boolean;
  evaluation_model: string;
  default_threshold: number;
  api_key_configured: boolean;
}

export interface QAMetrics {
  total_evaluations: number;
  total_passed: number;
  total_failed: number;
  pass_rate: number;
  avg_overall_score: number;
  avg_intent_completion: number | null;
  avg_tool_usage: number | null;
  avg_compliance: number | null;
  avg_response_quality: number | null;
  avg_coherence: number | null;
  avg_relevance: number | null;
  avg_groundedness: number | null;
  avg_fluency: number | null;
  total_cost_cents: number;
}

export interface DashboardMetrics {
  total_evaluations: number;
  passed_count: number;
  failed_count: number;
  pass_rate: number;
  average_score: number;
  score_breakdown: {
    intent_completion: number;
    tool_usage: number;
    compliance: number;
    response_quality: number;
  };
  quality_metrics: {
    coherence: number;
    relevance: number;
    groundedness: number;
    fluency: number;
  };
  sentiment_distribution: Record<string, number>;
  latency: {
    p50_ms: number;
    p90_ms: number;
    p95_ms: number;
  };
  period_days: number;
}

export interface TrendData {
  dates: string[];
  values: number[];
  metric: string;
}

export interface FailureReason {
  reason: string;
  count: number;
}

export interface AgentComparison {
  agent_id: string;
  total_evaluations: number;
  average_score: number;
  pass_rate: number;
}

export interface TestScenario {
  id: string;
  name: string;
  description: string | null;
  category: string;
  difficulty: string;
  caller_persona: Record<string, unknown> | null;
  initial_message: string | null;
  expected_behaviors: string[] | null;
  failure_conditions: string[] | null;
  success_criteria: Record<string, unknown> | null;
  max_turns: number;
  is_built_in: boolean;
  is_active: boolean;
  created_at: string;
}

export interface TestRun {
  id: string;
  agent_id: string;
  status: "pending" | "running" | "completed" | "failed";
  total_scenarios: number;
  passed_scenarios: number;
  failed_scenarios: number;
  pass_rate: number | null;
  results: Record<string, unknown>[] | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface StartTestRunParams {
  agent_id: string;
  scenario_ids?: string[];
  category?: string;
}

// =============================================================================
// QA Status
// =============================================================================

/**
 * Get QA system status
 */
export async function getQAStatus(): Promise<QAStatus> {
  const response = await fetch(`${API_BASE}/api/v1/qa/status`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch QA status");
  }

  return response.json();
}

// =============================================================================
// Evaluations
// =============================================================================

/**
 * List evaluations with pagination and filtering
 */
export async function listEvaluations(
  params: ListEvaluationsParams = {}
): Promise<CallEvaluationListResponse> {
  const searchParams = new URLSearchParams();
  if (params.page) searchParams.set("page", params.page.toString());
  if (params.page_size) searchParams.set("page_size", params.page_size.toString());
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.workspace_id) searchParams.set("workspace_id", params.workspace_id);
  if (params.passed !== undefined) searchParams.set("passed", params.passed.toString());

  const response = await fetch(`${API_BASE}/api/v1/qa/evaluations?${searchParams.toString()}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch evaluations");
  }

  return response.json();
}

/**
 * Get a specific evaluation
 */
export async function getEvaluation(evaluationId: string): Promise<CallEvaluation> {
  const response = await fetch(`${API_BASE}/api/v1/qa/evaluations/${evaluationId}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch evaluation");
  }

  return response.json();
}

/**
 * Get evaluation for a specific call
 */
export async function getCallEvaluation(callId: string): Promise<CallEvaluation> {
  const response = await fetch(`${API_BASE}/api/v1/qa/calls/${callId}/evaluation`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch call evaluation");
  }

  return response.json();
}

/**
 * Trigger manual evaluation for a call
 */
export async function evaluateCall(
  callId: string
): Promise<{ message: string; evaluation_id: string | null; queued: boolean }> {
  const response = await fetch(`${API_BASE}/api/v1/qa/evaluate`, {
    method: "POST",
    headers: {
      ...getAuthHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ call_id: callId }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to trigger evaluation");
  }

  return response.json();
}

// =============================================================================
// Metrics
// =============================================================================

/**
 * Get aggregated QA metrics
 */
export async function getQAMetrics(params: {
  agent_id?: string;
  workspace_id?: string;
}): Promise<QAMetrics> {
  const searchParams = new URLSearchParams();
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.workspace_id) searchParams.set("workspace_id", params.workspace_id);

  const response = await fetch(`${API_BASE}/api/v1/qa/metrics?${searchParams.toString()}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch QA metrics");
  }

  return response.json();
}

// =============================================================================
// Dashboard
// =============================================================================

/**
 * Get dashboard metrics
 */
export async function getDashboardMetrics(params: {
  agent_id?: string;
  workspace_id?: string;
  days?: number;
}): Promise<DashboardMetrics> {
  const searchParams = new URLSearchParams();
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.workspace_id) searchParams.set("workspace_id", params.workspace_id);
  if (params.days) searchParams.set("days", params.days.toString());

  const response = await fetch(
    `${API_BASE}/api/v1/qa/dashboard/metrics?${searchParams.toString()}`,
    {
      headers: getAuthHeaders(),
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch dashboard metrics");
  }

  return response.json();
}

/**
 * Get trend data for charts
 */
export async function getTrends(params: {
  agent_id?: string;
  workspace_id?: string;
  metric?: string;
  days?: number;
}): Promise<TrendData> {
  const searchParams = new URLSearchParams();
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.workspace_id) searchParams.set("workspace_id", params.workspace_id);
  if (params.metric) searchParams.set("metric", params.metric);
  if (params.days) searchParams.set("days", params.days.toString());

  const response = await fetch(
    `${API_BASE}/api/v1/qa/dashboard/trends?${searchParams.toString()}`,
    {
      headers: getAuthHeaders(),
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch trends");
  }

  return response.json();
}

/**
 * Get top failure reasons
 */
export async function getFailureReasons(params: {
  agent_id?: string;
  workspace_id?: string;
  days?: number;
  limit?: number;
}): Promise<FailureReason[]> {
  const searchParams = new URLSearchParams();
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.workspace_id) searchParams.set("workspace_id", params.workspace_id);
  if (params.days) searchParams.set("days", params.days.toString());
  if (params.limit) searchParams.set("limit", params.limit.toString());

  const response = await fetch(
    `${API_BASE}/api/v1/qa/dashboard/failure-reasons?${searchParams.toString()}`,
    {
      headers: getAuthHeaders(),
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch failure reasons");
  }

  return response.json();
}

/**
 * Get agent comparison stats
 */
export async function getAgentComparison(params: {
  workspace_id: string;
  days?: number;
}): Promise<AgentComparison[]> {
  const searchParams = new URLSearchParams();
  searchParams.set("workspace_id", params.workspace_id);
  if (params.days) searchParams.set("days", params.days.toString());

  const response = await fetch(
    `${API_BASE}/api/v1/qa/dashboard/agent-comparison?${searchParams.toString()}`,
    {
      headers: getAuthHeaders(),
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch agent comparison");
  }

  return response.json();
}

// =============================================================================
// Test Scenarios
// =============================================================================

/**
 * List test scenarios
 */
export async function listScenarios(params: {
  category?: string;
  built_in_only?: boolean;
}): Promise<TestScenario[]> {
  const searchParams = new URLSearchParams();
  if (params.category) searchParams.set("category", params.category);
  if (params.built_in_only !== undefined) {
    searchParams.set("built_in_only", params.built_in_only.toString());
  }

  const response = await fetch(
    `${API_BASE}/api/v1/testing/scenarios?${searchParams.toString()}`,
    {
      headers: getAuthHeaders(),
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch scenarios");
  }

  // Backend returns { scenarios: [...], total, page, ... } - extract scenarios array
  const data = await response.json();
  return data.scenarios ?? data;
}

/**
 * Get a specific test scenario
 */
export async function getScenario(scenarioId: string): Promise<TestScenario> {
  const response = await fetch(`${API_BASE}/api/v1/testing/scenarios/${scenarioId}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch scenario");
  }

  return response.json();
}

// =============================================================================
// Test Runs
// =============================================================================

/**
 * Start a new test run
 */
export async function startTestRun(params: StartTestRunParams): Promise<TestRun> {
  const response = await fetch(`${API_BASE}/api/v1/testing/runs`, {
    method: "POST",
    headers: {
      ...getAuthHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(params),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to start test run");
  }

  return response.json();
}

/**
 * List test runs
 */
export async function listTestRuns(params: {
  agent_id?: string;
  status?: string;
}): Promise<TestRun[]> {
  const searchParams = new URLSearchParams();
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.status) searchParams.set("status", params.status);

  const response = await fetch(
    `${API_BASE}/api/v1/testing/runs?${searchParams.toString()}`,
    {
      headers: getAuthHeaders(),
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch test runs");
  }

  // Backend returns { runs: [...], total, page, ... } - extract runs array
  const data = await response.json();
  return data.runs ?? data;
}

/**
 * Get a specific test run
 */
export async function getTestRun(runId: string): Promise<TestRun> {
  const response = await fetch(`${API_BASE}/api/v1/testing/runs/${runId}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch test run");
  }

  return response.json();
}
