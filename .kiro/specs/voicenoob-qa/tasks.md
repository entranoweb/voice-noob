# VoiceNoob QA Testing Framework - Tasks

## Reference Documents

#[[file:VOICENOOB_QA_SPRINT_PLAN.md]]
#[[file:VOICENOOB_QA_IMPLEMENTATION_CORRECTIONS.md]]
#[[file:VOICENOOB_QA_INDUSTRY_PATTERNS_INTEGRATION.md]]

## Task Overview

| Week | Focus | Tasks |
|------|-------|-------|
| 1 | Post-Call Evaluation | Tasks 1-8, **8.5 (Tests)** |
| 2 | Pre-Deployment Testing | Tasks 9-14 |
| 3 | Dashboard & Monitoring | Tasks 15-20, **20.5 (Tests)** |
| 4 | Auto-Remediation | Tasks 21-24 |

**Note:** Tasks 8.5 and 20.5 are CRITICAL testing tasks added to ensure production reliability.

---

## Week 1: Post-Call Evaluation Engine

### Task 1: Add Dependencies and Feature Flags
- [ ] Add `anthropic>=0.40.0` to `voice-noob/backend/pyproject.toml`
- [ ] Add QA feature flags to `voice-noob/backend/app/core/config.py`:
  - `QA_ENABLED: bool = False`
  - `QA_AUTO_EVALUATE: bool = True`
  - `QA_EVALUATION_MODEL: str = "claude-sonnet-4-20250514"`
  - `QA_DEFAULT_THRESHOLD: int = 70`
  - `QA_MAX_CONCURRENT_EVALUATIONS: int = 5`
  - `QA_ALERT_ON_FAILURE: bool = True`
  - `QA_ENABLE_LATENCY_TRACKING: bool = True`
  - `QA_ENABLE_TURN_ANALYSIS: bool = True`
  - `QA_ENABLE_QUALITY_METRICS: bool = True`
  - `ANTHROPIC_API_KEY: str | None = None`
- [ ] Run `uv add anthropic` in backend directory

### Task 2: Create Migration 016 - Call Evaluations Table
- [ ] Create `voice-noob/backend/migrations/versions/016_add_call_evaluations.py`
- [ ] Define `call_evaluations` table with:
  - Foreign key to `call_records.id` (CORRECTED: not `calls.id`)
  - Core scores: overall, intent_completion, tool_usage, compliance, response_quality
  - Quality metrics: coherence, relevance, groundedness, fluency (Promptflow pattern)
  - Sentiment fields: overall_sentiment, sentiment_score, sentiment_progression, escalation_risk
  - Latency tracking: latency_p50_ms, latency_p90_ms, latency_p95_ms (Retell pattern)
  - Audio quality: audio_quality_score, background_noise_detected, vad_metrics
  - Analysis JSONB: objectives_detected, failure_reasons, recommendations, turn_analysis, criteria_scores
  - Metadata: evaluation_model, evaluation_latency_ms, evaluation_cost_cents
- [ ] Create indexes for call_id, agent_id, workspace_id, passed, created_at, overall_score
- [ ] Set `down_revision = '015_azure_openai'` (this is the revision ID from 015_add_azure_openai_fields.py)

### Task 3: Create CallEvaluation Model
- [ ] Create `voice-noob/backend/app/models/call_evaluation.py`
- [ ] Define `CallEvaluation` class with all fields from migration
- [ ] Add relationship to `CallRecord` (CORRECTED: not `Call`)
- [ ] Add relationships to `Agent` and `Workspace`
- [ ] Implement `to_dict()` method for API responses
- [ ] Export from `voice-noob/backend/app/models/__init__.py`

### Task 4: Update CallRecord Model
- [ ] Add evaluation relationship to `voice-noob/backend/app/models/call_record.py`:
  ```python
  evaluation = relationship("CallEvaluation", back_populates="call", uselist=False)
  ```

### Task 5: Create QA Pydantic Schemas
- [ ] Define schemas INLINE in `voice-noob/backend/app/api/qa.py` (NO separate schemas folder - follow existing pattern):
  - `CallEvaluationResponse` - API response for evaluation
  - `CallEvaluationDetailResponse` - Detailed evaluation with all fields
  - `WorkspaceQASettings` - QA settings from workspace.settings JSON
  - `UpdateWorkspaceQASettings` - Update QA settings
  - `EvaluateCallRequest` - Request to trigger evaluation
  - `AgentStatsResponse` - Agent QA statistics
- [ ] Use `model_config = {"from_attributes": True}` for ORM compatibility

### Task 6: Create Evaluation Prompts
- [ ] Create `voice-noob/backend/app/services/qa/__init__.py`
- [ ] Create `voice-noob/backend/app/services/qa/prompts.py`
- [ ] Define `EVALUATION_PROMPT` with ONLY fields that exist in Agent model:
  - Agent configuration: `name`, `system_prompt`, `enabled_tools` (list of IDs)
  - Call information: `duration_seconds`, `direction`, `transcript`
  - ⚠️ DO NOT include `knowledge_base` or `compliance_rules` - these fields don't exist!
  - Evaluation criteria: Intent Completion, Tool Usage, Compliance, Response Quality, Sentiment
  - JSON output format with objectives_detected, scores, failure_reasons, recommendations
- [ ] Define `QUICK_EVALUATION_PROMPT` for fast/cheap evaluations
- [ ] Define `TURN_ANALYSIS_PROMPT` for real-time monitoring

### Task 7: Create Evaluator Service
- [ ] Create `voice-noob/backend/app/services/qa/evaluator.py`
- [ ] Implement `evaluate_call(call_id, quick_mode=False)` - NOTE: Creates its own DB session!
  - ⚠️ CRITICAL: Use `async with AsyncSessionLocal() as db:` - don't accept db as parameter
  - Fetch `CallRecord` with agent using `selectinload` (CORRECTED: not `Call`)
  - Check if transcript exists - skip if empty
  - Format transcript for prompt
  - Call Claude API with evaluation prompt using `anthropic.AsyncAnthropic`
  - Parse JSON response (handle markdown code blocks)
  - Calculate cost (Sonnet: $3/M input, $15/M output)
  - Create and save `CallEvaluation`
  - Trigger alert if failed
- [ ] Implement `trigger_qa_evaluation(call_id)` - Background task entry point:
  - ⚠️ CRITICAL: Only pass `call_id`, NOT db session (session will be closed!)
  - Create fresh DB session inside the function
  - Check `settings.QA_ENABLED`
  - Check `settings.ANTHROPIC_API_KEY`
  - Check workspace settings `qa_enabled` and `qa_auto_evaluate`
  - Call `evaluate_call(call_id)`
- [ ] Implement `batch_evaluate_calls(call_ids, concurrency=5)`
- [ ] Implement `get_agent_stats(db, agent_id, days=7)`
- [ ] Implement `format_transcript(transcript)` helper - transcript is already formatted as `[User]: ... [Assistant]: ...`
- [ ] Implement `parse_evaluation_response(response_text)` helper - strip markdown code blocks

### Task 8: Create Alert Service and Integration
- [ ] Create `voice-noob/backend/app/services/qa/alerts.py`
- [ ] Implement `send_failure_alert(db, evaluation)`:
  - Get workspace QA settings from `workspace.settings` JSON
  - Send to webhook if `qa_alert_webhook` configured
  - Send to Slack if `qa_slack_webhook` configured
- [ ] Create `voice-noob/backend/app/api/qa.py` with initial endpoints:
  - `POST /qa/evaluate/{call_id}` - Trigger evaluation
  - `GET /qa/evaluations` - List evaluations with filters
  - `GET /qa/evaluations/{id}` - Get evaluation details
  - `GET /qa/workspace/settings` - Get workspace QA settings
  - `PUT /qa/workspace/settings` - Update workspace QA settings
- [ ] Register QA router in `voice-noob/backend/app/main.py`
- [ ] Add evaluation trigger to `voice-noob/backend/app/api/telephony.py`:
  - In Twilio status callback (~line 879) after `await db.commit()` when `call_status == "completed"`:
    ```python
    # Trigger QA evaluation (runs in background with its own session)
    if call_status == "completed" and call_record:
        from app.services.qa.evaluator import trigger_qa_evaluation
        background_tasks.add_task(trigger_qa_evaluation, call_record.id)
    ```
  - In Telnyx status callback (~line 1099) after `call.hangup` handling:
    ```python
    # Trigger QA evaluation (runs in background with its own session)
    if event_type == "call.hangup" and call_record:
        from app.services.qa.evaluator import trigger_qa_evaluation
        background_tasks.add_task(trigger_qa_evaluation, call_record.id)
    ```
  - ⚠️ CRITICAL: Pass only `call_record.id`, NOT the db session!

### Task 8.5: Backend Tests for Week 1 (CRITICAL)

**Pattern Reference:** Follow existing patterns from `backend/tests/test_api/test_crm.py`, `backend/tests/conftest.py`

#### 8.5.1: Add QA Test Fixtures to `voice-noob/backend/tests/conftest.py`
- [ ] Add `sample_call_record_data` fixture (like `sample_contact_data`):
  ```python
  @pytest.fixture
  def sample_call_record_data() -> dict[str, Any]:
      """Sample call record data for QA testing."""
      return {
          "provider": "twilio",
          "provider_call_id": "CA" + "1" * 32,
          "direction": "inbound",
          "status": "completed",
          "from_number": "+14155551234",
          "to_number": "+14155555678",
          "duration_seconds": 180,
          "transcript": "[User]: I need to schedule an appointment\n[Assistant]: I'd be happy to help you schedule an appointment.",
          "started_at": datetime.now(UTC),
          "ended_at": datetime.now(UTC),
      }
  ```
- [ ] Add `create_test_call_record` factory fixture (like `create_test_contact`):
  ```python
  @pytest_asyncio.fixture
  async def create_test_call_record(test_session: AsyncSession) -> Any:
      async def _create_call_record(user_id: int, **kwargs: Any) -> CallRecord:
          from app.core.auth import user_id_to_uuid
          data = {**sample_defaults, "user_id": user_id_to_uuid(user_id)}
          data.update(kwargs)
          record = CallRecord(**data)
          test_session.add(record)
          await test_session.commit()
          await test_session.refresh(record)
          return record
      return _create_call_record
  ```
- [ ] Add `mock_anthropic_response` fixture:
  ```python
  @pytest.fixture
  def mock_anthropic_response() -> MagicMock:
      """Mock Claude API response for evaluation."""
      from unittest.mock import MagicMock
      return MagicMock(
          content=[MagicMock(text='''{
              "overall_score": 85,
              "intent_completion": 90,
              "tool_usage": 80,
              "compliance": 95,
              "response_quality": 75,
              "passed": true,
              "objectives_detected": ["schedule appointment"],
              "objectives_completed": ["schedule appointment"],
              "failure_reasons": [],
              "recommendations": []
          }''')],
          usage=MagicMock(input_tokens=500, output_tokens=200)
      )
  ```

#### 8.5.2: Create `voice-noob/backend/tests/test_models/test_call_evaluation.py`
- [ ] Test CallEvaluation model creation:
  ```python
  class TestCallEvaluationModel:
      @pytest.mark.asyncio
      async def test_create_evaluation(self, test_session: AsyncSession) -> None:
          # Create user, agent, call_record first
          evaluation = CallEvaluation(
              call_id=call_record.id,
              agent_id=agent.id,
              overall_score=85,
              passed=True,
              # ... other required fields
          )
          test_session.add(evaluation)
          await test_session.commit()
          assert evaluation.id is not None
  ```
- [ ] Test relationship to CallRecord
- [ ] Test passed/failed threshold logic (score >= 70 = passed)

#### 8.5.3: Create `voice-noob/backend/tests/test_api/test_qa.py`
- [ ] Use `authenticated_test_client` fixture pattern (returns `tuple[AsyncClient, User]`)
- [ ] Mock Anthropic using `patch` + `AsyncMock`:
  ```python
  class TestQAEndpoints:
      @pytest.mark.asyncio
      async def test_evaluate_call_success(
          self,
          authenticated_test_client: tuple[AsyncClient, User],
          create_test_call_record: Any,
          mock_anthropic_response: MagicMock,
      ) -> None:
          client, user = authenticated_test_client
          call_record = await create_test_call_record(user_id=user.id)

          with patch("app.services.qa.evaluator.AsyncAnthropic") as mock_client:
              mock_client.return_value.messages.create = AsyncMock(
                  return_value=mock_anthropic_response
              )

              response = await client.post(f"/api/v1/qa/evaluate/{call_record.id}")

              assert response.status_code == 200
              data = response.json()
              assert "overall_score" in data
              assert data["passed"] is True
  ```
- [ ] Test missing transcript returns 400:
  ```python
  async def test_evaluate_call_no_transcript(
      self,
      authenticated_test_client: tuple[AsyncClient, User],
      create_test_call_record: Any,
  ) -> None:
      client, user = authenticated_test_client
      call_record = await create_test_call_record(user_id=user.id, transcript=None)

      response = await client.post(f"/api/v1/qa/evaluate/{call_record.id}")

      assert response.status_code == 400
      assert "transcript" in response.json()["detail"].lower()
  ```
- [ ] Test `GET /qa/evaluations` list with filters
- [ ] Test `GET /qa/evaluations/{id}` returns correct evaluation
- [ ] Test workspace isolation (user can't see other user's evaluations)
- [ ] Test feature flag disabled (mock `settings.QA_ENABLED = False`):
  ```python
  async def test_qa_disabled_returns_error(
      self,
      authenticated_test_client: tuple[AsyncClient, User],
  ) -> None:
      client, user = authenticated_test_client

      with patch("app.core.config.settings.QA_ENABLED", False):
          response = await client.post("/api/v1/qa/evaluate/some-uuid")
          assert response.status_code in [404, 503]  # Feature disabled
  ```

#### 8.5.4: Create `voice-noob/backend/tests/test_services/test_evaluator.py`
- [ ] Test `format_transcript()` helper preserves format
- [ ] Test `parse_evaluation_response()` with valid JSON:
  ```python
  def test_parse_valid_json() -> None:
      response = '{"overall_score": 85, "passed": true}'
      result = parse_evaluation_response(response)
      assert result["overall_score"] == 85
  ```
- [ ] Test `parse_evaluation_response()` with markdown code blocks:
  ```python
  def test_parse_markdown_wrapped_json() -> None:
      response = '```json\n{"overall_score": 85}\n```'
      result = parse_evaluation_response(response)
      assert result["overall_score"] == 85
  ```
- [ ] Test `parse_evaluation_response()` with invalid JSON returns error dict
- [ ] Test cost calculation: `(input_tokens * 3 + output_tokens * 15) / 1_000_000 * 100`

#### 8.5.5: Run Backend Tests
```bash
# Run all QA tests
cd voice-noob/backend
uv run pytest tests/test_models/test_call_evaluation.py -v
uv run pytest tests/test_api/test_qa.py -v
uv run pytest tests/test_services/test_evaluator.py -v

# Run with coverage (minimum 80% for new files)
uv run pytest tests/ -k "qa or evaluation" --cov=app/services/qa --cov=app/api/qa --cov-report=term-missing --cov-fail-under=80
```

---

## Week 2: Pre-Deployment Testing

### Task 9: Create Migration 017 - Test Scenarios Table
- [ ] Create `voice-noob/backend/migrations/versions/017_add_test_scenarios.py`
- [ ] Define `test_scenarios` table with:
  - workspace_id FK
  - name, description, category
  - persona, initial_message
  - expected_behaviors, failure_conditions (JSONB)
  - success_criteria (JSONB)
  - timeout_seconds, max_turns
  - is_active, is_builtin
- [ ] Define `test_runs` table with:
  - workspace_id, agent_id FKs
  - scenario_ids (JSONB)
  - status, total_scenarios, passed_scenarios, failed_scenarios
  - overall_pass_rate, scenario_results (JSONB)
  - started_at, completed_at, duration_seconds
- [ ] Create indexes for workspace_id, category, status, agent_id
- [ ] Set `down_revision = '016_call_evaluations'` (use the revision ID you set in 016)

### Task 10: Create TestScenario and TestRun Models
- [ ] Create `voice-noob/backend/app/models/test_scenario.py`
- [ ] Define `TestScenario` class with all fields
- [ ] Define `TestRun` class with all fields
- [ ] Add relationships to Workspace and Agent
- [ ] Export from `voice-noob/backend/app/models/__init__.py`

### Task 11: Create 12 Built-in Edge Case Scenarios
- [ ] Create `voice-noob/backend/app/services/qa/scenarios/__init__.py`
- [ ] Create `voice-noob/backend/app/services/qa/scenarios/edge_cases.py`
- [ ] Define `BUILTIN_SCENARIOS` list with 12 scenarios:
  - hp_001: Simple Appointment Booking (happy_path)
  - ec_001: Angry Customer Escalation (edge_case)
  - ec_002: Confused Elderly Caller (edge_case)
  - ec_003: Background Noise (edge_case)
  - ec_004: Multi-Intent Request (edge_case)
  - ec_005: Out-of-Scope Request (edge_case)
  - ec_006: Callback Request (edge_case)
  - ec_007: Language Barrier (edge_case)
  - ec_008: Technical Difficulties (edge_case)
  - st_001: Rapid-Fire Questions (stress)
  - st_002: Long Silence (stress)
  - cp_001: PII Handling (compliance)
- [ ] Implement `seed_builtin_scenarios(db, workspace_id)` function

### Task 12: Create AI Test Caller Service
- [ ] Create `voice-noob/backend/app/services/qa/test_caller.py`
- [ ] Implement `AITestCaller` class:
  - `__init__(scenario, agent)` - Initialize with scenario and agent
  - `execute_scenario()` - Run full conversation simulation
  - `generate_next_message(agent_response)` - Generate user response based on persona
  - `check_completion(conversation)` - Check if scenario complete or failed
  - `compile_results()` - Create TestResult with conversation and evaluation
- [ ] Use Claude API to simulate user based on persona
- [ ] Track turn count and timeout

### Task 13: Create Test Runner Service
- [ ] Create `voice-noob/backend/app/services/qa/test_runner.py`
- [ ] Implement `execute_test_run(db, test_run_id)`:
  - Load test run and scenarios
  - For each scenario, create AITestCaller and execute
  - Evaluate each conversation
  - Update test run with results
- [ ] Implement `execute_scenario(db, scenario, agent)`:
  - Create AITestCaller
  - Execute scenario
  - Return ScenarioResult

### Task 14: Add Test Scenario API Endpoints
- [ ] Add to `voice-noob/backend/app/api/qa.py`:
  - `GET /qa/scenarios` - List scenarios (include builtin)
  - `POST /qa/scenarios` - Create custom scenario
  - `GET /qa/scenarios/{id}` - Get scenario details
  - `PUT /qa/scenarios/{id}` - Update scenario
  - `DELETE /qa/scenarios/{id}` - Delete scenario
  - `POST /qa/test-runs` - Start test run
  - `GET /qa/test-runs` - List test runs
  - `GET /qa/test-runs/{id}` - Get test run results
- [ ] Add Pydantic schemas for test scenarios and runs

---

## Week 3: Dashboard & Monitoring

### Task 15: Create Dashboard Metrics Service
- [ ] Create `voice-noob/backend/app/services/qa/dashboard.py`
- [ ] Implement `get_dashboard_metrics(db, workspace_id, agent_id, days)`:
  - Total evaluations, pass rate, average scores
  - Score breakdowns (intent, tool, compliance, quality)
  - Quality metrics (coherence, relevance, groundedness, fluency)
  - Sentiment distribution
  - Latency percentiles (p50, p90, p95)
- [ ] Implement `get_trends(db, workspace_id, agent_id, metric, days)`:
  - Daily aggregated scores
  - Pass rate trends
- [ ] Implement `get_top_failure_reasons(db, workspace_id, agent_id, days, limit)`:
  - Aggregate failure_reasons from evaluations
  - Return with counts and trends

### Task 16: Create Alert System
- [ ] Extend `voice-noob/backend/app/services/qa/alerts.py`:
  - `check_score_drop_alert(db, workspace_id, agent_id)` - Alert on significant score drop
  - `check_failure_spike_alert(db, workspace_id, agent_id)` - Alert on failure rate increase
  - `create_alert(db, alert_type, severity, agent_id, message)` - Create alert record
- [ ] Add alert model if needed (or use JSONB in workspace)
- [ ] Implement webhook signature verification (HMAC-SHA256)

### Task 17: Add Dashboard API Endpoints
- [ ] Add to `voice-noob/backend/app/api/qa.py`:
  - `GET /qa/dashboard/metrics` - Dashboard metrics
  - `GET /qa/dashboard/trends` - Trend data for charts
  - `GET /qa/alerts` - Get QA alerts
  - `POST /qa/alerts/{id}/acknowledge` - Acknowledge alert
- [ ] Add Pydantic schemas:
  - `DashboardMetrics`
  - `TrendData`
  - `RealTimeAlert`

### Task 18: Create Frontend QA API Client and Dependencies
- [ ] Install recharts: `cd voice-noob/frontend && npm install recharts`
- [ ] Create `voice-noob/frontend/src/lib/api/qa.ts` following pattern from `calls.ts`:
  - `listEvaluations(params)` - List evaluations with filters
  - `getEvaluation(id)` - Get evaluation details
  - `getDashboardMetrics(params)` - Get dashboard metrics
  - `getTrends(params)` - Get trend data for charts
  - `listScenarios()` - List test scenarios
  - `startTestRun(params)` - Start a test run
  - `getTestRun(id)` - Get test run results
  - `getRemediationSuggestions(agentId)` - Get remediation suggestions
- [ ] Define TypeScript interfaces matching backend schemas

### Task 19: Create Frontend QA Dashboard Page
- [ ] Create `voice-noob/frontend/src/app/dashboard/qa/page.tsx` following pattern from `dashboard/page.tsx`:
  - Use `"use client"` directive
  - Use `useQuery` from `@tanstack/react-query` for data fetching
  - Summary cards: Pass Rate, Avg Score, Total Evaluations, Failed Calls
  - Recent evaluations table with pass/fail badges
  - Top failure reasons list
- [ ] Create `voice-noob/frontend/src/app/dashboard/qa/loading.tsx` with Skeleton components
- [ ] Add agent filter dropdown using existing `Select` component
- [ ] Add date range selector (7d, 30d, 90d)

### Task 20: Create Frontend QA Components
- [ ] Create `voice-noob/frontend/src/components/qa/evaluation-list.tsx`:
  - Table using existing `Table` component
  - Pass/fail `Badge` components (green/red)
  - Score display with color coding
  - Link to evaluation details
- [ ] Create `voice-noob/frontend/src/components/qa/qa-metrics-chart.tsx`:
  - Line chart for score trends using recharts
  - Bar chart for score breakdown
  - Responsive design
- [ ] Create `voice-noob/frontend/src/components/qa/test-runner.tsx`:
  - Scenario multi-select
  - Agent selection dropdown
  - Run test button
  - Progress indicator
  - Results display with pass/fail per scenario
- [ ] Update `voice-noob/frontend/src/components/app-sidebar.tsx`:
  - Add QA Dashboard link with CheckCircle or Shield icon

### Task 20.5: Frontend Tests for QA (CRITICAL)

**Pattern Reference:** Follow existing patterns from `frontend/tests/mocks/handlers.ts`, `frontend/src/lib/__tests__/api.test.ts`

#### 20.5.1: Add QA Mock Data to `voice-noob/frontend/tests/mocks/data.ts`
```typescript
export const mockEvaluations = [
  {
    id: "eval-1",
    call_id: "call-1",
    agent_id: "agent-1",
    overall_score: 85,
    intent_completion: 90,
    tool_usage: 80,
    compliance: 95,
    response_quality: 75,
    passed: true,
    created_at: "2024-01-15T10:00:00Z",
  },
  {
    id: "eval-2",
    call_id: "call-2",
    agent_id: "agent-1",
    overall_score: 55,
    intent_completion: 50,
    tool_usage: 60,
    compliance: 70,
    response_quality: 40,
    passed: false,
    failure_reasons: ["Intent not completed", "Slow response"],
    created_at: "2024-01-14T10:00:00Z",
  },
];

export const mockDashboardMetrics = {
  total_evaluations: 150,
  pass_rate: 0.82,
  average_score: 78.5,
  score_breakdown: {
    intent_completion: 82,
    tool_usage: 75,
    compliance: 88,
    response_quality: 72,
  },
  top_failure_reasons: [
    { reason: "Intent not completed", count: 15 },
    { reason: "Compliance violation", count: 8 },
    { reason: "Slow response time", count: 5 },
  ],
};
```

#### 20.5.2: Add QA Handlers to `voice-noob/frontend/tests/mocks/handlers.ts`
```typescript
// Add to existing handlers array:

// QA Evaluations
http.get(`${API_URL}/api/v1/qa/evaluations`, () => {
  return HttpResponse.json(mockEvaluations);
}),

http.get(`${API_URL}/api/v1/qa/evaluations/:id`, ({ params }) => {
  const evaluation = mockEvaluations.find((e) => e.id === params.id);
  if (!evaluation) {
    return HttpResponse.json({ detail: "Evaluation not found" }, { status: 404 });
  }
  return HttpResponse.json(evaluation);
}),

http.post(`${API_URL}/api/v1/qa/evaluate/:callId`, () => {
  return HttpResponse.json(mockEvaluations[0], { status: 200 });
}),

// QA Dashboard
http.get(`${API_URL}/api/v1/qa/dashboard/metrics`, () => {
  return HttpResponse.json(mockDashboardMetrics);
}),

http.get(`${API_URL}/api/v1/qa/dashboard/trends`, () => {
  return HttpResponse.json({
    dates: ["2024-01-10", "2024-01-11", "2024-01-12"],
    scores: [75, 78, 82],
    pass_rates: [0.78, 0.80, 0.85],
  });
}),
```

#### 20.5.3: Create `voice-noob/frontend/src/lib/__tests__/qa.test.ts`
```typescript
import { describe, it, expect, beforeEach, vi } from "vitest";
import { listEvaluations, getDashboardMetrics, getEvaluation } from "../api/qa";

describe("QA API Client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("listEvaluations returns evaluations array", async () => {
    const result = await listEvaluations();
    expect(Array.isArray(result)).toBe(true);
    expect(result[0]).toHaveProperty("overall_score");
    expect(result[0]).toHaveProperty("passed");
  });

  it("getDashboardMetrics returns metrics object", async () => {
    const result = await getDashboardMetrics({ days: 7 });
    expect(result).toHaveProperty("total_evaluations");
    expect(result).toHaveProperty("pass_rate");
    expect(result).toHaveProperty("average_score");
  });

  it("getEvaluation returns single evaluation", async () => {
    const result = await getEvaluation("eval-1");
    expect(result.id).toBe("eval-1");
    expect(result.overall_score).toBeGreaterThan(0);
  });

  it("getEvaluation handles 404", async () => {
    await expect(getEvaluation("nonexistent")).rejects.toThrow();
  });
});
```

#### 20.5.4: Create `voice-noob/frontend/src/components/qa/__tests__/evaluation-list.test.tsx`
```typescript
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EvaluationList } from "../evaluation-list";
import { mockEvaluations } from "../../../../tests/mocks/data";

describe("EvaluationList", () => {
  it("renders pass badge (green) for passed=true", () => {
    render(<EvaluationList evaluations={[mockEvaluations[0]]} />);
    const badge = screen.getByText("Pass");
    expect(badge).toHaveClass("bg-green"); // or check actual class
  });

  it("renders fail badge (red) for passed=false", () => {
    render(<EvaluationList evaluations={[mockEvaluations[1]]} />);
    const badge = screen.getByText("Fail");
    expect(badge).toHaveClass("bg-red"); // or check actual class
  });

  it("displays score with correct color coding", () => {
    render(<EvaluationList evaluations={mockEvaluations} />);
    // Score 85 should be green
    const highScore = screen.getByText("85");
    expect(highScore).toBeInTheDocument();
    // Score 55 should be yellow/amber
    const lowScore = screen.getByText("55");
    expect(lowScore).toBeInTheDocument();
  });

  it("renders empty state when no evaluations", () => {
    render(<EvaluationList evaluations={[]} />);
    expect(screen.getByText(/no evaluations/i)).toBeInTheDocument();
  });
});
```

#### 20.5.5: Create `voice-noob/frontend/src/app/dashboard/qa/__tests__/page.test.tsx`
```typescript
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import QADashboardPage from "../page";

// Wrap with QueryClientProvider for tests
const renderWithProviders = (component: React.ReactNode) => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      {component}
    </QueryClientProvider>
  );
};

describe("QA Dashboard Page", () => {
  it("renders loading state initially", () => {
    renderWithProviders(<QADashboardPage />);
    expect(screen.getByTestId("loading-skeleton")).toBeInTheDocument();
  });

  it("renders metrics cards after loading", async () => {
    renderWithProviders(<QADashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/pass rate/i)).toBeInTheDocument();
      expect(screen.getByText(/total evaluations/i)).toBeInTheDocument();
    });
  });

  it("renders top failure reasons", async () => {
    renderWithProviders(<QADashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/intent not completed/i)).toBeInTheDocument();
    });
  });
});
```

#### 20.5.6: Run Frontend Tests
```bash
# Run all QA tests
cd voice-noob/frontend
npm test -- --run src/lib/__tests__/qa.test.ts
npm test -- --run src/components/qa/__tests__/
npm test -- --run src/app/dashboard/qa/__tests__/

# Run all tests with coverage
npm test -- --coverage
```

---

## Week 4: Auto-Remediation

### Task 21: Create Remediation Engine
- [ ] Create `voice-noob/backend/app/services/qa/remediation.py`
- [ ] Implement `AutoRemediationEngine` class:
  - `analyze_failures(agent_id, evaluations, min_sample_size)` - Analyze failure patterns
  - `categorize_failures(evaluations)` - Group failures by type
  - `generate_suggestion(category, failures, agent)` - Generate prompt fix
  - `estimate_improvement(suggestions)` - Estimate overall improvement
- [ ] Use Claude API to analyze failures and generate suggestions
- [ ] Return `RemediationReport` with suggestions

### Task 22: Create Remediation Schemas
- [ ] Add to `voice-noob/backend/app/api/qa.py` (inline with other schemas):
  - `RemediationSuggestion`:
    - root_cause, suggestion_type, current_text, suggested_text
    - placement, estimated_improvement_percent, confidence, reasoning
  - `RemediationReport`:
    - agent_id, analysis_period_days, total_failures
    - failure_categories, suggestions, estimated_improvement

### Task 23: Add Remediation API Endpoints
- [ ] Add to `voice-noob/backend/app/api/qa.py`:
  - `GET /qa/remediation/{agent_id}` - Get remediation suggestions
  - `POST /qa/remediation/{agent_id}/apply/{suggestion_index}` - Apply suggestion
- [ ] Implement apply logic:
  - Backup current prompt
  - Apply suggested modification
  - Track applied remediation

### Task 24: Create Frontend Remediation Panel
- [ ] Create `voice-noob/frontend/src/components/qa/remediation-panel.tsx`:
  - Display failure pattern analysis
  - Show suggestions with confidence scores
  - Preview prompt changes
  - Apply button with confirmation
  - Track improvement after application

---

## Validation Checklist

### Before Starting
- [ ] Read VOICENOOB_QA_SPRINT_PLAN.md completely
- [ ] Read VOICENOOB_QA_IMPLEMENTATION_CORRECTIONS.md completely
- [ ] Read VOICENOOB_QA_INDUSTRY_PATTERNS_INTEGRATION.md completely
- [ ] Verify on `synthiqvoice` branch

### Before Each PR
- [ ] All existing tests pass: `cd backend && uv run pytest`
- [ ] All frontend tests pass: `cd frontend && npm test`
- [ ] New code has tests (minimum 80% coverage for new files)
- [ ] Mock external APIs (Anthropic) - never hit real API in tests
- [ ] Feature flag exists for new behavior
- [ ] No changes to protected files (gpt_realtime.py, telephony_ws.py core)
- [ ] Model references use `CallRecord` not `Call`
- [ ] Table references use `call_records` not `calls`
- [ ] Workspace QA settings use JSON field, not new columns

### Search & Replace Reference
| Find | Replace With |
|------|--------------|
| `from app.models.call import Call` | `from app.models.call_record import CallRecord` |
| `ForeignKey("calls.id"` | `ForeignKey("call_records.id"` |
| `relationship("Call"` | `relationship("CallRecord"` |
| `workspace.qa_enabled` | `workspace.settings.get("qa_enabled", False)` |
