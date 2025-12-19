# VoiceNoob QA Testing Framework - Requirements

## Overview

Build a production-grade QA/Testing layer for the VoiceNoob voice AI platform that provides automated post-call evaluation, pre-deployment testing, real-time monitoring, and auto-remediation capabilities.

## Reference Documents

#[[file:VOICENOOB_QA_SPRINT_PLAN.md]]
#[[file:VOICENOOB_QA_IMPLEMENTATION_CORRECTIONS.md]]
#[[file:VOICENOOB_QA_INDUSTRY_PATTERNS_INTEGRATION.md]]

## Goals

1. **Match ReachAll.ai Phase 1** - Post-call evaluation with Claude API
2. **Beat ReachAll.ai Phase 2** - Pre-deployment testing (they ship Jan 9)
3. **Add differentiating features** - Real-time monitoring, dashboard
4. **Deliver auto-remediation** - Their Phase 3 is 2-3 months away

## Requirements

### Functional Requirements

#### FR-1: Post-Call Evaluation Engine (Week 1)
- FR-1.1: Automatically evaluate every completed call within 10 seconds
- FR-1.2: Score calls on 4 dimensions: Intent Completion, Tool Usage, Compliance, Response Quality (0-100 each)
- FR-1.3: Detect multiple user intents per call and track completion of each
- FR-1.4: Analyze user sentiment (positive/negative/neutral) with -1.0 to 1.0 score
- FR-1.5: Determine pass/fail status based on configurable threshold (default 70)
- FR-1.6: Store detailed evaluation results including objectives detected, failure reasons, and recommendations
- FR-1.7: Send alerts via webhook/Slack when calls fail evaluation

#### FR-2: Pre-Deployment Testing (Week 2)
- FR-2.1: Provide 12 built-in edge case test scenarios covering happy path, edge cases, stress tests, and compliance
- FR-2.2: Support custom test scenario creation with persona, initial message, expected behaviors, and failure conditions
- FR-2.3: Implement AI test caller that simulates conversations using Claude API
- FR-2.4: Execute test runs with multiple scenarios against an agent
- FR-2.5: Track test run results with pass/fail per scenario and overall pass rate
- FR-2.6: Support scenario categories: happy_path, edge_case, stress, compliance

#### FR-3: QA Dashboard & Monitoring (Week 3)
- FR-3.1: Display overall QA metrics: total evaluations, pass rate, average scores
- FR-3.2: Show score trends over time (daily/weekly)
- FR-3.3: Display top failure reasons with counts
- FR-3.4: Provide agent-specific QA views with comparison
- FR-3.5: Show test scenario pass rates by category
- FR-3.6: Support real-time alerts for score drops, failure spikes, compliance breaches

#### FR-4: Auto-Remediation (Week 4)
- FR-4.1: Analyze failure patterns across evaluations
- FR-4.2: Generate specific prompt improvement suggestions using Claude API
- FR-4.3: Provide estimated improvement percentage for each suggestion
- FR-4.4: Support one-click application of remediation suggestions
- FR-4.5: Track applied remediations and actual improvement

### Non-Functional Requirements

#### NFR-1: Performance
- NFR-1.1: Evaluation latency < 10 seconds per call
- NFR-1.2: Support 5 concurrent evaluations by default
- NFR-1.3: Dashboard metrics load within 2 seconds

#### NFR-2: Cost
- NFR-2.1: Target ~$0.003 per evaluation using Claude Sonnet
- NFR-2.2: Track evaluation cost in cents per call

#### NFR-3: Reliability
- NFR-3.1: All QA features must be behind feature flags
- NFR-3.2: QA failures must not affect core voice call functionality
- NFR-3.3: Support graceful degradation when Anthropic API is unavailable

#### NFR-4: Security
- NFR-4.1: Workspace isolation for all QA data
- NFR-4.2: Webhook signature verification for external integrations
- NFR-4.3: No PII exposure in evaluation results

### Technical Constraints

#### TC-1: Model/Table Corrections (CRITICAL)
- TC-1.1: Use `CallRecord` model (not `Call`)
- TC-1.2: Reference `call_records` table (not `calls`)
- TC-1.3: Use workspace.settings JSON field for QA settings (not new columns)

#### TC-2: Migration Requirements
- TC-2.1: Migrations must start at `016_*` (after `015_add_azure_openai_fields.py`)
- TC-2.2: Foreign keys must reference `call_records.id`

#### TC-3: Dependencies
- TC-3.1: Add `anthropic>=0.40.0` to pyproject.toml

#### TC-4: Protected Files (DO NOT MODIFY)
- TC-4.1: `backend/app/services/gpt_realtime.py` - Core voice session
- TC-4.2: `backend/app/api/telephony_ws.py` - WebSocket handlers
- TC-4.3: `backend/app/services/circuit_breaker.py` - Already complete
- TC-4.4: `backend/app/db/session.py` - Working config
- TC-4.5: `backend/app/db/redis.py` - Working config

#### TC-5: Additive Development
- TC-5.1: Only add new files or extend existing files
- TC-5.2: Do not modify existing function signatures
- TC-5.3: Do not change existing behavior
- TC-5.4: Feature flags for all new features (OFF by default)

## User Stories

### US-1: Post-Call Evaluation
As a voice AI platform operator, I want every call to be automatically evaluated so that I can identify quality issues without manual review.

**Acceptance Criteria:**
- Evaluation triggers automatically when call completes
- Scores appear in dashboard within 10 seconds
- Failed calls trigger alerts to configured webhooks

### US-2: Pre-Deployment Testing
As a voice AI developer, I want to test my agent against edge cases before deploying so that I can catch issues before they affect real customers.

**Acceptance Criteria:**
- Can run 12 built-in edge case scenarios
- Can create custom test scenarios
- Test results show pass/fail with detailed analysis

### US-3: QA Dashboard
As a QA manager, I want to see QA metrics and trends so that I can track agent quality over time.

**Acceptance Criteria:**
- Dashboard shows pass rate, average scores, trends
- Can filter by agent and time period
- Top failure reasons are highlighted

### US-4: Auto-Remediation
As a voice AI developer, I want AI-generated suggestions to fix common failures so that I can improve my agent faster.

**Acceptance Criteria:**
- System analyzes failure patterns
- Generates specific prompt modifications
- Shows estimated improvement percentage

## Out of Scope

- Real-time audio quality analysis (VAD metrics) - requires audio processing
- Load testing with concurrent calls - infrastructure dependent
- Multi-language evaluation - English only for MVP
- Integration with external QA platforms
