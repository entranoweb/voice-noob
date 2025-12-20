# VoiceNoob QA Testing Framework - Design

## Reference Documents

#[[file:VOICENOOB_QA_SPRINT_PLAN.md]]
#[[file:VOICENOOB_QA_IMPLEMENTATION_CORRECTIONS.md]]
#[[file:VOICENOOB_QA_INDUSTRY_PATTERNS_INTEGRATION.md]]

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    VOICENOOB PLATFORM (Existing)                 │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    QA LAYER (NEW)                        │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │    │
│  │  │  Post-Call  │  │    Pre-     │  │   Real-     │      │    │
│  │  │  Evaluator  │  │ Deployment  │  │   Time      │      │    │
│  │  │  (Week 1)   │  │  Testing    │  │ Monitoring  │      │    │
│  │  │             │  │  (Week 2)   │  │  (Week 3)   │      │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘      │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │    │
│  │  │    Auto-    │  │  Dashboard  │  │   Alert     │      │    │
│  │  │ Remediation │  │    UI       │  │   System    │      │    │
│  │  │  (Week 4)   │  │  (Week 3)   │  │  (Week 1)   │      │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘      │    │
│  └─────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              EXISTING VOICENOOB SERVICES                 │    │
│  │  - GPTRealtimeSession / Pipecat Pipeline                │    │
│  │  - Telephony (Telnyx/Twilio)                            │    │
│  │  - Tool Registry / CRM / Appointments                   │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

## File Structure

```
backend/
├── app/
│   ├── api/
│   │   ├── qa.py                          # NEW: QA API endpoints
│   │   └── telephony.py                   # MODIFY: Add evaluation trigger
│   ├── models/
│   │   ├── call_evaluation.py             # NEW: Evaluation model
│   │   └── test_scenario.py               # NEW: TestScenario + TestRun models
│   │   # NOTE: Pydantic schemas defined INLINE in api/qa.py (no separate schemas folder)
│   ├── services/
│   │   └── qa/                            # NEW: QA service directory
│   │       ├── __init__.py
│   │       ├── evaluator.py               # Post-call evaluation engine
│   │       ├── prompts.py                 # Evaluation prompts
│   │       ├── alerts.py                  # Alert/webhook system
│   │       ├── test_caller.py             # AI test caller
│   │       ├── test_runner.py             # Test execution engine
│   │       ├── remediation.py             # Auto-remediation engine
│   │       └── scenarios/
│   │           ├── __init__.py
│   │           └── edge_cases.py          # 12 built-in edge cases
│   ├── core/
│   │   └── config.py                      # MODIFY: Add QA feature flags
│   └── main.py                            # MODIFY: Register QA router
├── migrations/
│   └── versions/
│       ├── 016_add_call_evaluations.py    # NEW: Evaluation table (down_revision='015_azure_openai')
│       └── 017_add_test_scenarios.py      # NEW: Test scenarios table
│
frontend/src/
├── app/dashboard/
│   └── qa/                                # NEW: QA dashboard route
│       ├── page.tsx
│       ├── loading.tsx
│       └── [evaluationId]/page.tsx
├── components/qa/                         # NEW: QA components
│   ├── evaluation-list.tsx
│   ├── qa-metrics-chart.tsx
│   ├── test-runner.tsx
│   └── remediation-panel.tsx
└── lib/api/
    └── qa.ts                              # NEW: QA API client
```

## Data Models

### CallEvaluation Model

```python
class CallEvaluation(Base):
    __tablename__ = "call_evaluations"
    
    id: UUID                           # Primary key
    call_id: UUID                      # FK to call_records.id (CORRECTED)
    agent_id: UUID                     # FK to agents.id
    workspace_id: UUID                 # FK to workspaces.id
    
    # Core scores (0-100)
    overall_score: int
    intent_completion_score: int | None
    tool_usage_score: int | None
    compliance_score: int | None
    response_quality_score: int | None
    
    # Quality metrics (Promptflow pattern, 0.0-1.0)
    coherence_score: float | None
    relevance_score: float | None
    groundedness_score: float | None
    fluency_score: float | None
    
    # Sentiment (Retell pattern)
    overall_sentiment: str | None      # positive, negative, neutral
    sentiment_score: float | None      # -1.0 to 1.0
    sentiment_progression: str | None  # improving, stable, declining
    escalation_risk: int | None        # 0-100
    
    # Latency tracking (Retell pattern)
    latency_p50_ms: int | None
    latency_p90_ms: int | None
    latency_p95_ms: int | None
    
    # Audio quality
    audio_quality_score: int = 100
    background_noise_detected: bool = False
    vad_metrics: dict | None           # Pipecat pattern
    
    # Pass/fail
    passed: bool
    
    # Analysis (JSONB)
    objectives_detected: list          # Multi-intent detection
    failure_reasons: list
    recommendations: list
    turn_analysis: dict | None
    criteria_scores: dict | None       # LlamaIndex pattern
    evaluation_raw: dict | None
    
    # Metadata
    evaluation_model: str = "claude-sonnet-4-20250514"
    evaluation_latency_ms: int | None
    evaluation_cost_cents: int | None
    
    # Timestamps
    created_at: datetime
    updated_at: datetime
    
    # Relationships
    call: relationship("CallRecord")   # CORRECTED: CallRecord not Call
    agent: relationship("Agent")
    workspace: relationship("Workspace")
```

### TestScenario Model

```python
class TestScenario(Base):
    __tablename__ = "test_scenarios"
    
    id: UUID
    workspace_id: UUID                 # FK to workspaces.id
    
    # Basic info
    name: str
    description: str | None
    category: str                      # happy_path, edge_case, stress, compliance
    
    # Scenario configuration
    persona: str                       # "Angry customer", "Confused elderly", etc.
    initial_message: str
    expected_behaviors: list           # What agent should do
    failure_conditions: list           # What agent should NOT do
    success_criteria: dict             # Structured success criteria
    
    # Execution limits
    timeout_seconds: int = 300
    max_turns: int = 20
    
    # Status
    is_active: bool = True
    is_builtin: bool = False
    
    # Timestamps
    created_at: datetime
    updated_at: datetime
```

### TestRun Model

```python
class TestRun(Base):
    __tablename__ = "test_runs"
    
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    
    # Configuration
    scenario_ids: list[UUID]
    status: str                        # pending, running, completed, failed
    
    # Results
    total_scenarios: int
    passed_scenarios: int
    failed_scenarios: int
    overall_pass_rate: float | None
    scenario_results: list             # Detailed per-scenario results
    
    # Timing
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: int | None
    
    # Timestamps
    created_at: datetime
    updated_at: datetime
```

## API Design

### QA Endpoints

```
POST   /api/qa/evaluate/{call_id}           # Trigger evaluation for a call
GET    /api/qa/evaluations                  # List evaluations with filters
GET    /api/qa/evaluations/{id}             # Get evaluation details

GET    /api/qa/scenarios                    # List test scenarios
POST   /api/qa/scenarios                    # Create custom scenario
GET    /api/qa/scenarios/{id}               # Get scenario details
PUT    /api/qa/scenarios/{id}               # Update scenario
DELETE /api/qa/scenarios/{id}               # Delete scenario

POST   /api/qa/test-runs                    # Start a test run
GET    /api/qa/test-runs                    # List test runs
GET    /api/qa/test-runs/{id}               # Get test run results

GET    /api/qa/dashboard/metrics            # Dashboard metrics
GET    /api/qa/dashboard/trends             # Trend data for charts
GET    /api/qa/alerts                       # Get QA alerts
POST   /api/qa/alerts/{id}/acknowledge      # Acknowledge alert

GET    /api/qa/remediation/{agent_id}       # Get remediation suggestions
POST   /api/qa/remediation/{agent_id}/apply # Apply a suggestion

GET    /api/qa/workspace/settings           # Get workspace QA settings
PUT    /api/qa/workspace/settings           # Update workspace QA settings
```

## Service Design

### EvaluatorService

```python
class EvaluatorService:
    """Post-call evaluation using Claude API."""
    
    async def evaluate_call(db, call_id, quick_mode=False) -> CallEvaluation:
        """Evaluate a completed call."""
        # 1. Fetch call with transcript from CallRecord
        # 2. Fetch agent config (system_prompt, tools)
        # 3. Build evaluation prompt
        # 4. Call Claude API
        # 5. Parse response and create CallEvaluation
        # 6. Send alert if failed
        
    async def batch_evaluate_calls(db, call_ids, concurrency=5) -> list[CallEvaluation]:
        """Evaluate multiple calls concurrently."""
        
    async def get_agent_stats(db, agent_id, days=7) -> dict:
        """Get QA statistics for an agent."""
```

### TestRunnerService

```python
class TestRunnerService:
    """Execute test scenarios against agents."""
    
    async def execute_test_run(db, test_run_id) -> TestRun:
        """Execute all scenarios in a test run."""
        # 1. Load test run and scenarios
        # 2. For each scenario, run AI test caller
        # 3. Evaluate each conversation
        # 4. Update test run with results
        
    async def execute_scenario(db, scenario, agent) -> ScenarioResult:
        """Execute a single test scenario."""
```

### AITestCaller

```python
class AITestCaller:
    """AI-powered test caller using Claude API."""
    
    async def execute_scenario(scenario, agent) -> TestResult:
        """Simulate a conversation based on scenario."""
        # 1. Initialize with persona and initial message
        # 2. Loop: get agent response, generate next user message
        # 3. Check for completion or failure conditions
        # 4. Return conversation and evaluation
```

### RemediationEngine

```python
class RemediationEngine:
    """Auto-remediation suggestions using Claude API."""
    
    async def analyze_failures(agent_id, evaluations) -> RemediationReport:
        """Analyze failure patterns and suggest fixes."""
        # 1. Categorize failures
        # 2. For each category with enough samples
        # 3. Generate specific prompt modification
        # 4. Estimate improvement percentage
        
    async def apply_suggestion(agent_id, suggestion_index) -> Agent:
        """Apply a remediation suggestion to agent prompt."""
```

## Feature Flags

```python
# backend/app/core/config.py

class Settings(BaseSettings):
    # Master switch
    QA_ENABLED: bool = False
    
    # Auto-evaluate calls on completion
    QA_AUTO_EVALUATE: bool = True
    
    # Evaluation configuration
    QA_EVALUATION_MODEL: str = "claude-sonnet-4-20250514"
    QA_DEFAULT_THRESHOLD: int = 70
    QA_MAX_CONCURRENT_EVALUATIONS: int = 5
    
    # Industry pattern features
    QA_ENABLE_LATENCY_TRACKING: bool = True
    QA_ENABLE_TURN_ANALYSIS: bool = True
    QA_ENABLE_QUALITY_METRICS: bool = True
    
    # Alerting
    QA_ALERT_ON_FAILURE: bool = True
    QA_ALERT_SCORE_THRESHOLD: int = 70
    
    # Auto-remediation
    QA_ENABLE_AUTO_REMEDIATION: bool = False
    QA_REMEDIATION_MIN_SAMPLE_SIZE: int = 10
    
    # Test scenarios
    QA_ENABLE_TEST_SCENARIOS: bool = True
    QA_TEST_CALLER_MODEL: str = "claude-sonnet-4-20250514"
    
    # Anthropic API (required for QA)
    ANTHROPIC_API_KEY: str | None = None
```

## Workspace QA Settings (JSON Field)

```python
# Access via workspace.settings JSON field (not new columns)

workspace.settings.get("qa_enabled", False)
workspace.settings.get("qa_auto_evaluate", True)
workspace.settings.get("qa_alert_webhook", None)
workspace.settings.get("qa_alert_threshold", 70)
workspace.settings.get("qa_slack_webhook", None)
```

## 12 Built-in Edge Case Scenarios

| ID | Name | Category | Description |
|----|------|----------|-------------|
| hp_001 | Simple Appointment Booking | happy_path | Standard booking flow |
| ec_001 | Angry Customer Escalation | edge_case | Customer demanding refund |
| ec_002 | Confused Elderly Caller | edge_case | Hard of hearing, confused |
| ec_003 | Background Noise | edge_case | Noisy environment |
| ec_004 | Multi-Intent Request | edge_case | Multiple requests at once |
| ec_005 | Out-of-Scope Request | edge_case | Unsupported service request |
| ec_006 | Callback Request | edge_case | Customer can't talk now |
| ec_007 | Language Barrier | edge_case | Non-native English speaker |
| ec_008 | Technical Difficulties | edge_case | App issues reported |
| st_001 | Rapid-Fire Questions | stress | Many questions quickly |
| st_002 | Long Silence | stress | Customer goes silent |
| cp_001 | PII Handling | compliance | SSN volunteered |

## Integration Points

### Call Completion Hook

```python
# In backend/app/api/telephony.py

# Twilio status callback - add after call_record update:
if call_status == "completed" and call_record:
    from app.services.qa.evaluator import trigger_qa_evaluation
    background_tasks.add_task(trigger_qa_evaluation, call_record.id)

# Telnyx webhook - add after call_record update:
if event_type == "call.hangup" and call_record:
    from app.services.qa.evaluator import trigger_qa_evaluation
    background_tasks.add_task(trigger_qa_evaluation, call_record.id)

# NOTE: Pass ONLY call_record.id - the background task creates its own DB session
```

### Main Router Registration

```python
# In backend/app/main.py

from app.api.qa import router as qa_router
app.include_router(qa_router, prefix="/api")
```

## Error Handling

- Evaluation failures create error evaluation with `passed=False`
- API errors return graceful error responses
- Feature flag checks prevent execution when disabled
- Missing Anthropic API key logs warning and skips evaluation

## Security Considerations

- Workspace isolation on all queries
- Webhook signature verification (HMAC-SHA256)
- No PII in evaluation results
- Feature flags default to OFF


## Codebase Verification (December 18, 2025)

### Verified Correct
- ✅ Model is `CallRecord` in `voice-noob/backend/app/models/call_record.py`
- ✅ Table is `call_records` (not `calls`)
- ✅ Workspace uses JSON `settings` field for flexible config
- ✅ Latest migration is `015_azure_openai` (revision ID in 015_add_azure_openai_fields.py)
- ✅ Migration chain: `014_embed_settings` → `c1a2629e6aad` → `015_azure_openai`
- ✅ Config at `voice-noob/backend/app/core/config.py` uses Pydantic BaseSettings
- ✅ Models exported from `voice-noob/backend/app/models/__init__.py`

### Integration Points Verified
- ✅ Twilio status callback at `telephony.py:822` - add QA trigger after `await db.commit()` when `call_status == "completed"`
- ✅ Telnyx status callback at `telephony.py:1028` - add QA trigger after `call.hangup` handling

### CallRecord Fields Available for Evaluation
```python
id: UUID
user_id: UUID
provider: str              # "twilio" or "telnyx"
provider_call_id: str
agent_id: UUID | None
workspace_id: UUID | None
direction: str             # "inbound" or "outbound"
status: str
from_number: str
to_number: str
duration_seconds: int
recording_url: str | None
transcript: str | None     # KEY: Used for evaluation
started_at: datetime
ended_at: datetime | None
```

### Workspace Settings Pattern
```python
# Access QA settings via JSON field (DO NOT add new columns)
workspace.settings.get("qa_enabled", False)
workspace.settings.get("qa_auto_evaluate", True)
workspace.settings.get("qa_alert_threshold", 70)
```

### Protected Files (DO NOT MODIFY core logic)
- `voice-noob/backend/app/services/gpt_realtime.py`
- `voice-noob/backend/app/api/telephony_ws.py`
- `voice-noob/backend/app/services/circuit_breaker.py`
- `voice-noob/backend/app/db/session.py`
- `voice-noob/backend/app/db/redis.py`


## Frontend Architecture (Verified December 18, 2025)

### Tech Stack
- **Framework:** Next.js 15 with App Router
- **State Management:** 
  - Zustand for global client state (see `src/lib/sidebar-store.ts`)
  - TanStack Query (`@tanstack/react-query`) for server state
- **API Client:** Fetch-based functions in `src/lib/api/` folder
- **UI Components:** Radix UI + shadcn/ui pattern
- **Styling:** Tailwind CSS + tailwindcss-animate
- **Icons:** lucide-react
- **Forms:** react-hook-form + zod validation
- **Charting:** None installed - need to add `recharts`

### File Structure (Frontend)

```
frontend/src/
├── app/
│   └── dashboard/
│       └── qa/                        # NEW: QA dashboard route
│           ├── page.tsx               # Main QA dashboard
│           ├── loading.tsx            # Loading skeleton
│           └── [evaluationId]/
│               └── page.tsx           # Evaluation detail view
├── components/
│   └── qa/                            # NEW: QA components
│       ├── evaluation-list.tsx
│       ├── qa-metrics-chart.tsx
│       ├── test-runner.tsx
│       ├── remediation-panel.tsx
│       └── qa-settings-dialog.tsx
└── lib/
    └── api/
        └── qa.ts                      # NEW: QA API client
```

### API Client Pattern (Follow Existing)

```typescript
// src/lib/api/qa.ts - Follow pattern from calls.ts

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function getAuthHeaders(): HeadersInit {
  if (typeof window === "undefined") return {};
  const token = localStorage.getItem("access_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface CallEvaluation {
  id: string;
  call_id: string;
  agent_id: string;
  overall_score: number;
  passed: boolean;
  overall_sentiment: string | null;
  failure_reasons: string[];
  recommendations: string[];
  created_at: string;
}

export interface QADashboardMetrics {
  total_evaluations: number;
  pass_rate: number;
  avg_score: number;
  avg_intent_score: number;
  avg_tool_score: number;
  avg_compliance_score: number;
  top_failure_reasons: { reason: string; count: number }[];
}

export async function listEvaluations(params: {
  agent_id?: string;
  passed?: boolean;
  limit?: number;
}): Promise<CallEvaluation[]> {
  const searchParams = new URLSearchParams();
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.passed !== undefined) searchParams.set("passed", String(params.passed));
  if (params.limit) searchParams.set("limit", String(params.limit));

  const response = await fetch(`${API_BASE}/api/qa/evaluations?${searchParams}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) throw new Error("Failed to fetch evaluations");
  return response.json();
}

export async function getDashboardMetrics(params: {
  agent_id?: string;
  days?: number;
}): Promise<QADashboardMetrics> {
  const searchParams = new URLSearchParams();
  if (params.agent_id) searchParams.set("agent_id", params.agent_id);
  if (params.days) searchParams.set("days", String(params.days));

  const response = await fetch(`${API_BASE}/api/qa/dashboard/metrics?${searchParams}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) throw new Error("Failed to fetch metrics");
  return response.json();
}
```

### Dashboard Page Pattern (Follow Existing)

```tsx
// src/app/dashboard/qa/page.tsx - Follow pattern from dashboard/page.tsx

"use client";

import { useQuery } from "@tanstack/react-query";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { CheckCircle, XCircle, TrendingUp, AlertTriangle } from "lucide-react";
import { getDashboardMetrics, listEvaluations } from "@/lib/api/qa";

export default function QADashboardPage() {
  const { data: metrics } = useQuery({
    queryKey: ["qa-metrics"],
    queryFn: () => getDashboardMetrics({ days: 7 }),
  });

  const { data: recentEvaluations = [] } = useQuery({
    queryKey: ["qa-evaluations-recent"],
    queryFn: () => listEvaluations({ limit: 10 }),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">QA Dashboard</h1>
          <p className="text-sm text-muted-foreground">Monitor agent quality and performance</p>
        </div>
        <Button size="sm">Run Tests</Button>
      </div>

      {/* Stats Cards - Match existing dashboard style */}
      <div className="grid gap-3 md:grid-cols-4">
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs text-muted-foreground">Pass Rate</p>
                <p className="text-lg font-semibold">{metrics?.pass_rate ?? 0}%</p>
              </div>
              <CheckCircle className="h-4 w-4 text-muted-foreground" />
            </div>
          </CardContent>
        </Card>
        {/* ... more cards */}
      </div>

      {/* Evaluations List */}
      {/* ... */}
    </div>
  );
}
```

### UI Components to Use (Already Available)
- `Card`, `CardContent` - Container cards
- `Button` - Actions
- `Badge` - Status indicators (pass/fail)
- `Table` - Evaluation lists
- `Tabs` - Dashboard sections
- `Select` - Agent/time filters
- `Dialog` - Settings, test runner modal
- `Skeleton` - Loading states

### New Dependency Required
```bash
# Add recharts for trend charts
cd voice-noob/frontend
npm install recharts
```

### Sidebar Navigation (Add QA Link)
Update `src/components/app-sidebar.tsx` to add QA dashboard link:
```tsx
{
  title: "QA Dashboard",
  url: "/dashboard/qa",
  icon: CheckCircle, // or Shield
}
```


## Critical Implementation Notes (Verified December 18, 2025)

### Transcript Format (Actual)
```python
# From GPTRealtimeSession.get_transcript():
"[User]: Hi I need to book an appointment\n\n[Assistant]: Of course! I can help..."
```
This format is clean and parseable for Claude evaluation.

### Agent Fields Available for Evaluation
| Field | Available | Notes |
|-------|-----------|-------|
| `system_prompt` | ✅ | Full prompt text |
| `name` | ✅ | Agent name |
| `enabled_tools` | ✅ | List of integration IDs |
| `enabled_tool_ids` | ✅ | Granular: `{integration_id: [tool_ids]}` |
| `knowledge_base` | ❌ | NOT in model - remove from prompt |
| `compliance_rules` | ❌ | NOT in model - derive from system_prompt |

### Potential Issues & Fixes

**1. No Knowledge Base Field**
```python
# WRONG - field doesn't exist:
knowledge_base_summary: {agent.knowledge_base}

# FIX - remove or use placeholder:
knowledge_base_summary: "Not configured"
```

**2. No Compliance Rules Field**
```python
# WRONG - field doesn't exist:
compliance_rules: {agent.compliance_rules}

# FIX - extract from system_prompt or omit:
compliance_rules: "See system prompt for agent guidelines"
```

**3. Background Task DB Session Issue**
```python
# WRONG - session may be closed:
background_tasks.add_task(evaluate_call, db, call_id)

# FIX - create new session in task:
async def evaluate_call(call_id: UUID):
    async with get_async_session() as db:
        # ... evaluation logic
```

**4. Empty Transcript Check**
```python
# Agent may have enable_transcript=False
# This check happens INSIDE trigger_qa_evaluation() after creating fresh session
async def trigger_qa_evaluation(call_id: UUID):
    async with AsyncSessionLocal() as db:
        call_record = await db.get(CallRecord, call_id)

        # Skip if no transcript
        if not call_record.transcript or not call_record.transcript.strip():
            logger.info("qa_skipped_no_transcript", call_id=call_id)
            return
```

**5. Tool Names Need Context**
```python
# enabled_tools = ["google_calendar", "crm"] - just IDs, not descriptions

# FIX - provide simple list or fetch tool registry:
tools_available: "google_calendar, crm (appointment booking, contact management)"
```

### Simplified Production Prompt (Recommended)

Given the actual data available, use this simplified but effective prompt:

```python
EVALUATION_PROMPT = '''You are evaluating a voice AI agent call.

## AGENT
Name: {agent_name}
System Prompt:
"""
{system_prompt}
"""

Tools Available: {enabled_tools}

## CALL
Duration: {duration_seconds} seconds
Direction: {direction}

## TRANSCRIPT
{transcript}

---

## EVALUATION CRITERIA

Score each 0-100:

1. **Intent Completion**: Did the agent identify and fulfill what the user wanted?
   - Detect all user intents (booking, questions, complaints, etc.)
   - Track if each was resolved

2. **Tool Usage**: Were tools used correctly?
   - Right tool for the task
   - Correct parameters
   - Results verified before confirming

3. **Compliance**: Did agent follow its system prompt guidelines?
   - Stayed in scope
   - No hallucinated information
   - Appropriate handling of edge cases

4. **Response Quality**: Were responses helpful and natural?
   - Clear and concise (appropriate for voice)
   - Professional tone
   - Addressed user's actual question

5. **Sentiment**: How did the user feel?
   - Track sentiment progression through call
   - Note any escalation

## OUTPUT (JSON only)

```json
{{
  "overall_score": <0-100>,
  "passed": <true if >= {threshold}>,
  "intent_completion_score": <0-100>,
  "tool_usage_score": <0-100>,
  "compliance_score": <0-100>,
  "response_quality_score": <0-100>,
  "objectives_detected": [
    {{"objective": "...", "completed": true/false, "notes": "..."}}
  ],
  "overall_sentiment": "positive|neutral|negative",
  "sentiment_score": <-1.0 to 1.0>,
  "failure_reasons": ["reason1", "reason2"],
  "recommendations": ["actionable suggestion 1", "actionable suggestion 2"],
  "summary": "2-3 sentence summary"
}}
```
'''
```

### Evaluation Trigger Pattern (Correct)

```python
# In telephony.py status callbacks

# DON'T pass db session to background task
# DO pass just the call_id

from app.services.qa.evaluator import trigger_qa_evaluation

# After call completion:
if call_status == "completed" and call_record:
    background_tasks.add_task(trigger_qa_evaluation, call_record.id)

# In evaluator.py:
async def trigger_qa_evaluation(call_id: UUID):
    """Background task - creates its own DB session."""
    from app.db.session import get_async_session
    
    async with get_async_session() as db:
        # Check feature flags
        if not settings.QA_ENABLED:
            return
        if not settings.ANTHROPIC_API_KEY:
            logger.warning("qa_skipped_no_api_key")
            return
        
        # Fetch call record
        call_record = await db.get(CallRecord, call_id)
        if not call_record:
            return
        
        # Skip if no transcript
        if not call_record.transcript:
            logger.info("qa_skipped_no_transcript", call_id=call_id)
            return
        
        # Check workspace settings
        workspace = await db.get(Workspace, call_record.workspace_id)
        if workspace and not workspace.settings.get("qa_enabled", False):
            return
        
        # Run evaluation
        await evaluate_call(db, call_id)
```

### What Will Work vs What Needs Adjustment

| Aspect | Status | Notes |
|--------|--------|-------|
| Transcript available | ✅ Works | Saved before status callback |
| Agent system_prompt | ✅ Works | Available in model |
| Agent tools | ⚠️ Adjust | Just IDs, no descriptions |
| Knowledge base | ❌ Remove | Field doesn't exist |
| Compliance rules | ❌ Remove | Field doesn't exist |
| DB session in background | ⚠️ Fix | Create new session |
| Empty transcript | ⚠️ Handle | Check before evaluation |
| Workspace settings | ✅ Works | JSON field pattern |


## Final Verification Summary (December 18, 2025)

### ✅ WILL WORK - Verified

| Component | Status | Evidence |
|-----------|--------|----------|
| Transcript format | ✅ | `[User]: ... [Assistant]: ...` from `get_transcript()` |
| Transcript timing | ✅ | Saved BEFORE status callback (WebSocket close → save → callback) |
| Agent.system_prompt | ✅ | Field exists in model |
| Agent.enabled_tools | ✅ | Field exists (list of integration IDs) |
| CallRecord.transcript | ✅ | Field exists, populated after call |
| Workspace.settings JSON | ✅ | Existing pattern for flexible config |
| Migration chain | ✅ | `015_azure_openai` is current head |
| Router registration | ✅ | Pattern: `app.include_router(qa.router)` |
| Pydantic schemas | ✅ | Inline in API files, not separate folder |

### ⚠️ CRITICAL FIXES REQUIRED

| Issue | Problem | Solution |
|-------|---------|----------|
| DB Session in Background Task | `background_tasks.add_task(fn, db, call_id)` - session closed! | Pass only `call_id`, create session inside task |
| Missing Agent Fields | Prompt references `knowledge_base`, `compliance_rules` | Remove from prompt - fields don't exist |
| Empty Transcript | `agent.enable_transcript` may be False | Check before evaluation |
| No schemas/ folder | Spec said create `schemas/qa.py` | Define inline in `api/qa.py` |

### Correct Background Task Pattern

```python
# In telephony.py - CORRECT:
background_tasks.add_task(trigger_qa_evaluation, call_record.id)

# In evaluator.py - CORRECT:
async def trigger_qa_evaluation(call_id: UUID):
    """Background task - creates its own DB session."""
    from app.db.session import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        try:
            # All evaluation logic here with fresh session
            await evaluate_call_internal(db, call_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
```

### Agent Fields Actually Available

```python
# From voice-noob/backend/app/models/agent.py
agent.name                    # ✅ Use
agent.system_prompt           # ✅ Use
agent.enabled_tools           # ✅ Use (list of integration IDs)
agent.enabled_tool_ids        # ✅ Use (dict: {integration: [tools]})
agent.language                # ✅ Use
agent.voice                   # Optional
agent.pricing_tier            # Optional

# NOT AVAILABLE - DO NOT USE:
agent.knowledge_base          # ❌ Doesn't exist
agent.compliance_rules        # ❌ Doesn't exist
```

### Pydantic Schema Pattern (Follow Existing)

```python
# In api/qa.py - NOT in a separate schemas folder

from pydantic import BaseModel

class CallEvaluationResponse(BaseModel):
    """Call evaluation response."""
    id: str
    call_id: str
    overall_score: int
    passed: bool
    # ... other fields
    
    model_config = {"from_attributes": True}
```

### Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Anthropic API rate limits | Low | Medium | Implement retry with backoff |
| Empty transcripts | Medium | Low | Skip evaluation, log warning |
| JSON parse failures | Low | Medium | Fallback to error evaluation |
| DB session issues | High if not fixed | High | Use pattern above |
| Feature flag not checked | Medium | High | Multiple check points |

### Go/No-Go Checklist

Before implementation:
- [x] Transcript format verified
- [x] Agent model fields verified
- [x] DB session pattern documented
- [x] Router registration pattern verified
- [x] Pydantic schema pattern verified
- [x] Migration chain verified
- [x] Integration points identified
- [x] Feature flags defined

**VERDICT: GO** - With the documented fixes applied, this implementation will work.


## Testing Strategy (Industry Research - December 18, 2025)

### Research Sources
- getzep/graphiti - AsyncAnthropic mocking pattern
- firebase/genkit - Mock response structure
- encode/httpx - MockTransport for webhook testing
- agentscope-ai/agentscope - Async client testing
- OpenHands/OpenHands - Background task testing
- langflow-ai/langflow - JSON response parsing tests

### Core Testing Principles

1. **NEVER hit real Anthropic API** - All tests use mocked `AsyncAnthropic`
2. **Test JSON parsing edge cases** - Valid JSON, markdown-wrapped, invalid JSON
3. **Test background task queueing** - Verify `add_task` called with correct args
4. **Test workspace isolation** - User can only see their workspace's evaluations
5. **Test feature flag behavior** - Disabled flag returns 404 or skips
6. **Test empty transcript handling** - Should skip evaluation gracefully

### Mock Patterns

#### 1. AsyncAnthropic Client Mock (from getzep/graphiti)

```python
from unittest.mock import patch, AsyncMock, MagicMock

@pytest.fixture
def mock_anthropic_client():
    """Mock AsyncAnthropic client - NEVER hit real API."""
    with patch('anthropic.AsyncAnthropic') as mock_client:
        mock_instance = mock_client.return_value
        
        # Mock successful response structure
        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            type='text',
            text='{"overall_score": 85, "passed": true, "intent_completion_score": 90}'
        )]
        mock_response.usage = MagicMock(input_tokens=500, output_tokens=200)
        mock_response.stop_reason = 'end_turn'
        
        mock_instance.messages.create = AsyncMock(return_value=mock_response)
        yield mock_instance
```

#### 2. Webhook Testing with MockTransport (from encode/httpx)

```python
import httpx

def test_webhook_delivery():
    webhook_called = False
    
    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal webhook_called
        webhook_called = True
        assert request.url.path == "/webhook"
        return httpx.Response(200, json={"status": "received"})
    
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        await send_webhook_alert(client, webhook_url, evaluation)
    
    assert webhook_called is True
```

#### 3. Background Task Testing (from OpenHands)

```python
from unittest.mock import MagicMock

def test_background_task_queued():
    mock_background_tasks = MagicMock()
    
    # Call endpoint that queues background task
    await endpoint_handler(background_tasks=mock_background_tasks)
    
    # Verify task was queued with correct args
    mock_background_tasks.add_task.assert_called_once_with(
        trigger_qa_evaluation,
        call_record.id  # Only ID, not session!
    )
```

### Test Coverage Requirements

| Component | Minimum Coverage | Critical Tests |
|-----------|------------------|----------------|
| `app/services/qa/evaluator.py` | 80% | `evaluate_call`, `parse_evaluation_response` |
| `app/services/qa/alerts.py` | 75% | `send_webhook_alert`, `send_slack_alert` |
| `app/api/qa.py` | 80% | All endpoints, workspace isolation |
| `app/models/call_evaluation.py` | 90% | Model creation, relationships |

### Test Commands

```bash
# Run all QA tests
cd voice-noob/backend
uv run pytest tests/ -k "qa or evaluation" -v

# Run with coverage
uv run pytest tests/ -k "qa or evaluation" \
  --cov=app/services/qa \
  --cov=app/api/qa \
  --cov=app/models/call_evaluation \
  --cov-report=term-missing \
  --cov-fail-under=80

# Run specific test file
uv run pytest tests/test_services/test_evaluator.py -v

# Run with verbose output for debugging
uv run pytest tests/test_api/test_qa.py -v -s --tb=long
```

### What Tests Will Catch

| Issue | Test Type | Example |
|-------|-----------|---------|
| Invalid JSON from Claude | Unit | `test_parse_evaluation_response_invalid_json` |
| Missing transcript | API | `test_evaluate_call_no_transcript` |
| Workspace data leak | API | `test_workspace_isolation` |
| Feature flag bypass | API | `test_qa_disabled_returns_error` |
| Background task args | Unit | `test_background_task_queued` |
| Webhook delivery | Integration | `test_webhook_delivery` |
| Score threshold logic | Model | `test_passed_threshold_logic` |

### What Tests Won't Catch (Manual Testing Required)

| Issue | Why | Manual Test |
|-------|-----|-------------|
| Claude response quality | Mocked responses | Test with real API in staging |
| Rate limits | Can't simulate | Monitor in production |
| Large transcript handling | Hard to generate | Test with 10+ minute call |
| Real webhook delivery | Mocked | Test with ngrok + real endpoint |
| UI rendering | No frontend tests | Visual inspection |

### Pre-Implementation Checklist

- [x] Mock patterns documented
- [x] Coverage requirements defined
- [x] Test commands documented
- [x] Edge cases identified
- [x] Manual testing gaps identified

**VERDICT: Testing strategy is production-ready.**
