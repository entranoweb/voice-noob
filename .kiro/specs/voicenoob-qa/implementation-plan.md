# VoiceNoob QA Testing Framework - Implementation Plan

## Spec Files Reference

| File | Purpose | Key Contents |
|------|---------|--------------|
| `requirements.md` | WHAT to build | 20 FRs, 11 NFRs, 14 Technical Constraints |
| `design.md` | HOW to build | Architecture, Models, API routes, Patterns |
| `tasks.md` | STEPS to build | 26 tasks across 4 weeks + testing tasks |

## Development Strategy

### Base Branch
- `synthiqvoice` - All development happens here or feature branches from it

### PR Strategy (Safe Incremental Merges)
```
synthiqvoice (base) ──┬── PR #1: Task 1-2 (Dependencies + Migration 016)
                      ├── PR #2: Task 3-4 (Models)
                      ├── PR #3: Task 5-7 (Schemas + Evaluator Service)
                      ├── PR #4: Task 8 (API + Integration)
                      ├── PR #5: Task 8.5 (Tests for Week 1)
                      ├── PR #6: Task 9-10 (Migration 017 + Test Models)
                      ├── PR #7: Task 11-13 (Scenarios + Test Caller)
                      ├── PR #8: Task 14 (Test API Endpoints)
                      ├── PR #9: Task 15-17 (Dashboard Backend)
                      ├── PR #10: Task 18-20 (Frontend Components)
                      ├── PR #11: Task 20.5 (Frontend Tests)
                      └── PR #12: Task 21-24 (Auto-Remediation)
```

### PR Review Process
1. Create feature branch from `synthiqvoice`
2. Implement 1-3 related tasks
3. Include tests for new code
4. Run `/check` (linting + type checks)
5. Create PR
6. Greptile reviews automatically
7. Only merge if approved

## Strict Constraints

### Protected Files (DO NOT MODIFY)
- `backend/app/services/gpt_realtime.py` - Core voice session
- `backend/app/api/telephony_ws.py` - WebSocket handlers
- `backend/app/services/circuit_breaker.py` - Already complete
- `backend/app/db/session.py` - Working config
- `backend/app/db/redis.py` - Working config

**Exception:** `telephony.py` gets 2 small additions (trigger lines) per Task 8

### Pattern Compliance

| Pattern | Correct | Incorrect |
|---------|---------|-----------|
| Model | `CallRecord` | `Call` |
| Table | `call_records` | `calls` |
| QA Settings | `workspace.settings.get("qa_enabled")` | `workspace.qa_enabled` |
| Schemas | Inline in API file | Separate `schemas/` folder |
| Background tasks | Pass only `call_id` | Pass `db` session |

### Testing Requirements
- Mock Anthropic API (never hit real API in tests)
- Minimum 80% coverage for new files
- Follow existing fixture patterns from `conftest.py`
- Use `authenticated_test_client` for API tests
- Use `patch` + `AsyncMock` for external services

## Week-by-Week Breakdown

### Week 1: Post-Call Evaluation Engine
- Tasks 1-8 + 8.5 (Tests)
- PRs: #1-5
- Key deliverables: Migration 016, CallEvaluation model, Evaluator service, QA API

### Week 2: Pre-Deployment Testing
- Tasks 9-14
- PRs: #6-8
- Key deliverables: Migration 017, 12 built-in scenarios, Test caller, Test runner

### Week 3: Dashboard & Monitoring
- Tasks 15-20 + 20.5 (Tests)
- PRs: #9-11
- Key deliverables: Dashboard metrics, Alerts, Frontend QA page

### Week 4: Auto-Remediation
- Tasks 21-24
- PR: #12
- Key deliverables: Remediation engine, Apply suggestions

## Quality Gates (Before Each PR)

```bash
# Backend
cd backend
uv run ruff check app tests --fix
uv run ruff format app tests
uv run mypy app
uv run pytest tests/ -v

# Frontend
cd frontend
npm run check
npm test
```

## Status

- [ ] Greptile integration ready
- [ ] First PR (Task 1-2) started
- [ ] Week 1 complete
- [ ] Week 2 complete
- [ ] Week 3 complete
- [ ] Week 4 complete
