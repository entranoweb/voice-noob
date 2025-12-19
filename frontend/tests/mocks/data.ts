export const mockContacts = [
  {
    id: 1,
    user_id: 1,
    first_name: "John",
    last_name: "Doe",
    email: "john@example.com",
    phone_number: "+15551234567",
    company_name: "Acme Corp",
    status: "new",
    tags: "sales,vip",
    notes: "Interested in premium tier",
  },
  {
    id: 2,
    user_id: 1,
    first_name: "Jane",
    last_name: "Smith",
    email: "jane@techstartup.com",
    phone_number: "+15559876543",
    company_name: "Tech Startup Inc",
    status: "qualified",
    tags: "enterprise,hot-lead",
    notes: "Ready to sign up",
  },
  {
    id: 3,
    user_id: 1,
    first_name: "Bob",
    last_name: "Johnson",
    email: "bob@example.com",
    phone_number: "+15555555555",
    company_name: null,
    status: "contacted",
    tags: null,
    notes: null,
  },
];

export const mockCRMStats = {
  total_contacts: 42,
  total_appointments: 15,
  total_calls: 103,
};

export const mockPricingTiers = [
  {
    id: "budget",
    name: "Budget",
    costPerHour: 0.86,
    costPerMinute: 0.0143,
  },
  {
    id: "balanced",
    name: "Balanced",
    costPerHour: 1.35,
    costPerMinute: 0.0225,
    recommended: true,
  },
  {
    id: "premium",
    name: "Premium",
    costPerHour: 1.92,
    costPerMinute: 0.032,
  },
];

// QA Mock Data
export const mockEvaluations = [
  {
    id: "eval-1",
    call_id: "call-1",
    agent_id: "agent-1",
    workspace_id: "workspace-1",
    overall_score: 85,
    intent_completion: 90,
    tool_usage: 80,
    compliance: 95,
    response_quality: 75,
    passed: true,
    coherence: 88,
    relevance: 85,
    groundedness: 90,
    fluency: 82,
    overall_sentiment: "positive",
    sentiment_score: 0.7,
    escalation_risk: 0.1,
    objectives_detected: ["schedule appointment"],
    objectives_completed: ["schedule appointment"],
    failure_reasons: null,
    recommendations: [],
    evaluation_model: "claude-sonnet-4-20250514",
    evaluation_latency_ms: 1500,
    evaluation_cost_cents: 0.3,
    created_at: "2024-01-15T10:00:00Z",
  },
  {
    id: "eval-2",
    call_id: "call-2",
    agent_id: "agent-1",
    workspace_id: "workspace-1",
    overall_score: 55,
    intent_completion: 50,
    tool_usage: 60,
    compliance: 70,
    response_quality: 40,
    passed: false,
    coherence: 55,
    relevance: 50,
    groundedness: 60,
    fluency: 55,
    overall_sentiment: "negative",
    sentiment_score: -0.3,
    escalation_risk: 0.6,
    objectives_detected: ["get order status", "speak to human"],
    objectives_completed: [],
    failure_reasons: ["Intent not completed", "Slow response"],
    recommendations: ["Improve response time", "Add escalation handling"],
    evaluation_model: "claude-sonnet-4-20250514",
    evaluation_latency_ms: 1800,
    evaluation_cost_cents: 0.35,
    created_at: "2024-01-14T10:00:00Z",
  },
];

export const mockDashboardMetrics = {
  total_evaluations: 150,
  passed_count: 123,
  failed_count: 27,
  pass_rate: 0.82,
  average_score: 78.5,
  score_breakdown: {
    intent_completion: 82,
    tool_usage: 75,
    compliance: 88,
    response_quality: 72,
  },
  quality_metrics: {
    coherence: 80,
    relevance: 78,
    groundedness: 82,
    fluency: 76,
  },
  sentiment_distribution: {
    positive: 90,
    neutral: 45,
    negative: 15,
  },
  latency: {
    p50_ms: 1200,
    p90_ms: 2000,
    p95_ms: 2500,
  },
  period_days: 7,
};

export const mockQAStatus = {
  enabled: true,
  auto_evaluate: true,
  evaluation_model: "claude-sonnet-4-20250514",
  default_threshold: 70,
  api_key_configured: true,
};

export const mockFailureReasons = [
  { reason: "Intent not completed", count: 15 },
  { reason: "Compliance violation", count: 8 },
  { reason: "Slow response time", count: 5 },
  { reason: "Tool usage error", count: 3 },
  { reason: "Escalation needed", count: 2 },
];

export const mockTrendData = {
  dates: ["2024-01-10", "2024-01-11", "2024-01-12", "2024-01-13", "2024-01-14"],
  values: [75, 78, 82, 80, 85],
  metric: "overall_score",
};
