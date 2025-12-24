# VoiceNoob QA Testing Framework - Implementation Guide

> **Purpose:** Single source of truth for implementing the QA Testing Framework. Follow this document to prevent context rot and maintain consistency across sessions.

---

## Quick Reference

| Item | Value |
|------|-------|
| **Base Branch** | `synthiqvoice` |
| **PR Target** | `synthiqvoice` |
| **Spec Location** | `.kiro/specs/voicenoob-qa/` |
| **Verification Tool** | Augment MCP (`mcp__auggie-mcp__codebase-retrieval`) |

---

## 1. Spec Files (READ THESE FIRST)

Before any implementation, read these files in order:

```
.kiro/specs/voicenoob-qa/
├── requirements.md          # WHAT to build (20 FRs, 11 NFRs, 14 constraints)
├── design.md                # HOW to build (architecture, models, patterns)
├── tasks.md                 # STEPS to build (26 tasks, 4 weeks)
└── implementation-plan.md   # PR strategy, quality gates
```

### File Purposes

| File | Read For | Key Sections |
|------|----------|--------------|
| `requirements.md` | Understanding scope | FR-*, NFR-*, TC-* (Technical Constraints) |
| `design.md` | Architecture decisions | Data Model, API Routes, Integration Points |
| `tasks.md` | Step-by-step execution | Week 1-4 tasks with code examples |
| `implementation-plan.md` | PR workflow | Git strategy, quality gates |

---

## 2. Git Strategy

### Branch Structure
```
main (protected)
  └── synthiqvoice (development base)
        ├── feature/qa-week1-deps        → PR #1
        ├── feature/qa-week1-models      → PR #2
        ├── feature/qa-week1-evaluator   → PR #3
        ├── feature/qa-week1-api         → PR #4
        ├── feature/qa-week1-tests       → PR #5
        └── ... (continue per task group)
```

### PR Workflow

1. **Create feature branch** from `synthiqvoice`:
   ```bash
   git checkout synthiqvoice
   git pull origin synthiqvoice
   git checkout -b feature/qa-week1-deps
   ```

2. **Implement tasks** (1-3 related tasks per PR)

3. **Run quality checks**:
   ```bash
   # Backend
   cd backend && uv run ruff check app tests --fix && uv run ruff format app tests && uv run mypy app && uv run pytest tests/ -v

   # Frontend
   cd frontend && npm run check && npm test
   ```

4. **Commit with descriptive message**:
   ```bash
   git add .
   git commit -m "feat(qa): add anthropic dependency and feature flags

   - Add anthropic>=0.40.0 to pyproject.toml
   - Add QA_* feature flags to config.py
   - Task 1 of QA Testing Framework

   Refs: .kiro/specs/voicenoob-qa/tasks.md#task-1"
   ```

5. **Push and create PR**:
   ```bash
   git push -u origin feature/qa-week1-deps
   gh pr create --base synthiqvoice --title "feat(qa): Week 1 - Dependencies and Feature Flags"
   ```

6. **PR gets reviewed** by code review tool

7. **Merge only after approval**

---

## 3. Critical Constraints (MUST FOLLOW)

### Model/Table Names
```python
# CORRECT
from app.models.call_record import CallRecord
ForeignKey("call_records.id")
relationship("CallRecord")

# WRONG - DO NOT USE
from app.models.call import Call
ForeignKey("calls.id")
relationship("Call")
```

### Workspace QA Settings
```python
# CORRECT - Use JSON field
qa_enabled = workspace.settings.get("qa_enabled", False)
qa_threshold = workspace.settings.get("qa_threshold", 70)

# WRONG - No column access
qa_enabled = workspace.qa_enabled  # Column doesn't exist!
```

### Background Tasks (Evaluator)
```python
# CORRECT - Pass only ID, create session inside
background_tasks.add_task(trigger_qa_evaluation, call_record.id)

async def trigger_qa_evaluation(call_id: UUID):
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await evaluate_call_internal(db, call_id)

# WRONG - Never pass db session to background task
background_tasks.add_task(trigger_qa_evaluation, db, call_record.id)  # Session will be closed!
```

### Pydantic Schemas
```python
# CORRECT - Inline in API file
# In app/api/qa.py
class CallEvaluationResponse(BaseModel):
    id: str
    overall_score: int
    passed: bool
    model_config = {"from_attributes": True}

# WRONG - No separate schemas folder
# Don't create app/schemas/qa.py
```

---

## 4. Protected Files (DO NOT MODIFY)

These files are stable and should not be changed:

| File | Reason |
|------|--------|
| `app/services/gpt_realtime.py` | Core voice session logic |
| `app/api/telephony_ws.py` | WebSocket handlers |
| `app/services/circuit_breaker.py` | Completed implementation |
| `app/db/session.py` | Working DB config |
| `app/db/redis.py` | Working Redis config |

**Exception:** `app/api/telephony.py` gets 2 small additions (QA trigger lines) per Task 8.

---

## 5. Verification with Auggie MCP

Before implementing, ALWAYS verify against codebase:

```
Use: mcp__auggie-mcp__codebase-retrieval

Example queries:
- "Show me the CallRecord model and its fields"
- "How are background tasks used in campaign_worker.py"
- "What is the pattern for Pydantic schemas in API files"
- "Show me the workspace.settings JSON field usage"
```

---

## 6. Testing Requirements

### Backend Tests (Task 8.5)
```bash
# Location
backend/tests/test_models/test_call_evaluation.py
backend/tests/test_api/test_qa.py
backend/tests/test_services/test_evaluator.py

# Run
cd backend
uv run pytest tests/ -k "qa or evaluation" --cov=app/services/qa --cov-fail-under=80
```

### Frontend Tests (Task 20.5)
```bash
# Location
frontend/tests/mocks/data.ts          # Mock data
frontend/tests/mocks/handlers.ts      # MSW handlers
frontend/src/lib/__tests__/qa.test.ts
frontend/src/components/qa/__tests__/

# Run
cd frontend && npm test
```

### Key Testing Rules
1. **Never hit real Claude API** - Always mock with `patch` + `AsyncMock`
2. **Minimum 80% coverage** for new files
3. **Use existing fixtures** from `conftest.py`
4. **Test error paths** - not just happy path

---

## 7. Task Execution Checklist

Before starting any task:
- [ ] Read the task in `tasks.md`
- [ ] Read related sections in `design.md`
- [ ] Verify patterns with Auggie MCP
- [ ] Create feature branch from `synthiqvoice`

After completing a task:
- [ ] Run `/check` (linting, types, tests)
- [ ] All tests pass
- [ ] No new warnings
- [ ] Commit with descriptive message
- [ ] Create PR to `synthiqvoice`

---

## 8. Week-by-Week Summary

| Week | Focus | Key Deliverables | PRs |
|------|-------|------------------|-----|
| 1 | Post-Call Evaluation | Migration 016, CallEvaluation model, Evaluator service, QA API | #1-5 |
| 2 | Pre-Deployment Testing | Migration 017, 12 scenarios, Test caller, Test runner | #6-8 |
| 3 | Dashboard & Monitoring | Metrics service, Alerts, Frontend QA page | #9-11 |
| 4 | Auto-Remediation | Remediation engine, Apply suggestions | #12 |

---

## 9. Quick Commands

```bash
# Switch to correct branch
git checkout synthiqvoice && git pull

# Create feature branch
git checkout -b feature/qa-<description>

# Run all backend checks
cd backend && uv run ruff check app tests --fix && uv run ruff format app tests && uv run mypy app && uv run pytest

# Run all frontend checks
cd frontend && npm run check && npm test

# Create PR
gh pr create --base synthiqvoice --title "feat(qa): <description>"

# View spec files
cat .kiro/specs/voicenoob-qa/tasks.md
```

---

## 10. Migration Chain

```
015_add_azure_openai_fields.py  ← Current HEAD
    ↓
016_add_call_evaluations.py     ← Week 1 (Task 2)
    ↓
017_add_test_scenarios.py       ← Week 2 (Task 9)
```

Always verify with:
```bash
cd backend && uv run alembic heads
```

---

## Remember

1. **Read specs first** - Don't code from memory
2. **Verify with Auggie** - Don't assume patterns
3. **Small PRs** - 1-3 tasks per PR
4. **Test everything** - Mock external APIs
5. **Follow constraints** - CallRecord, not Call

---

## 11. CRITICAL: Auggie MCP Verification Protocol

**BEFORE writing ANY code, ALWAYS use Auggie MCP to:**

### Pre-Implementation Checks
```
mcp__auggie-mcp__codebase-retrieval queries:

1. "Show me the exact file and pattern for <what you're about to create>"
2. "How is <similar feature> implemented in the codebase"
3. "What imports and dependencies does <target file> use"
4. "Show me the test patterns for <similar functionality>"
```

### Mandatory Verification Points

| Before You... | Query Auggie For... |
|---------------|---------------------|
| Create a model | "Show existing model patterns in app/models/" |
| Add an API route | "Show API route patterns in app/api/" |
| Write a service | "Show service patterns in app/services/" |
| Add a migration | "Show latest migration and its revision ID" |
| Create tests | "Show test patterns in tests/test_api/" |
| Add frontend component | "Show component patterns in src/components/" |
| Add frontend page | "Show page patterns in src/app/dashboard/" |

### Systematic Development Flow

```
┌─────────────────────────────────────────────────────────────┐
│  1. READ SPEC                                               │
│     └── Read task from tasks.md                             │
│                                                             │
│  2. VERIFY WITH AUGGIE                                      │
│     └── Query for existing patterns                         │
│     └── Query for related code                              │
│     └── Query for imports/dependencies                      │
│                                                             │
│  3. IMPLEMENT                                               │
│     └── Follow verified patterns EXACTLY                    │
│     └── Don't invent new patterns                           │
│     └── Don't break existing code                           │
│                                                             │
│  4. TEST LOCALLY                                            │
│     └── Run /check                                          │
│     └── Run relevant tests                                  │
│     └── Start server, check for runtime errors              │
│                                                             │
│  5. VERIFY AGAIN                                            │
│     └── Use Auggie to confirm integration points            │
│     └── Check nothing is broken                             │
│                                                             │
│  6. COMMIT & PR                                             │
│     └── Small, focused commits                              │
│     └── PR to synthiqvoice                                  │
└─────────────────────────────────────────────────────────────┘
```

### Golden Rules

1. **When in doubt, query Auggie** - Never guess or assume
2. **Match existing patterns** - Don't introduce new conventions
3. **Additive changes only** - Don't modify working code unless required
4. **One thing at a time** - Complete and verify before moving on
5. **If it breaks, rollback** - Don't push broken code

---

*Last updated: 2024-12-19*
*Branch: synthiqvoice*
