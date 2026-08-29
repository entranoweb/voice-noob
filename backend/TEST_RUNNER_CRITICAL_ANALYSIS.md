# Test Runner Robustness: Critical Analysis

**Date:** 2025-12-22
**Scope:** `test_runner.py`, `test_caller.py`, `resilience.py`, `testing.py`
**Verdict:** Production-Ready with 18 Critical Gaps

---

## Executive Summary

The QA testing implementation has **good foundations** (resilience patterns, circuit breaker, retry logic) but has **18 critical robustness gaps** that could cause cascading failures, stuck test runs, data loss, and poor user experience in production.

### Severity Breakdown
- **CRITICAL (Production Blockers):** 8 issues
- **HIGH (Fail Under Load):** 6 issues
- **MEDIUM (UX/Monitoring):** 4 issues

---

## CRITICAL ISSUES (Production Blockers)

### 1. Test Runs Can Get Stuck in "RUNNING" State Forever

**Location:** `test_runner.py:188-252`

**Problem:**
```python
test_run = TestRun(
    scenario_id=scenario_id,
    agent_id=agent_id,
    workspace_id=workspace_id,
    user_id=user_id,
    status=TestRunStatus.RUNNING.value,
    started_at=datetime.now(UTC),
)
self.db.add(test_run)
await self.db.commit()  # ← Status set to RUNNING
await self.db.refresh(test_run)

# ... test execution ...

except Exception as e:
    log.exception("test_run_failed", error=str(e))
    test_run.status = TestRunStatus.ERROR.value
    test_run.completed_at = datetime.now(UTC)
    test_run.error_message = str(e)
    await self.db.commit()
```

**What Can Go Wrong:**
- Process crash (OOM, SIGKILL, server restart) after commit → status stuck "running"
- Database connection lost during execution → can't update status
- Worker container killed → no cleanup
- If `await self.db.commit()` in exception handler fails → still stuck "running"

**Impact:**
- Users see perpetually "running" tests in UI
- Can't tell if test is actually running or stuck
- Database fills with zombie test runs
- No automatic cleanup mechanism

**Recommendation:**
```python
# Add a timeout-based cleanup job
async def cleanup_abandoned_tests():
    """Mark tests stuck in RUNNING for >15 mins as ERROR."""
    cutoff = datetime.now(UTC) - timedelta(minutes=15)
    await db.execute(
        update(TestRun)
        .where(
            TestRun.status == TestRunStatus.RUNNING.value,
            TestRun.started_at < cutoff
        )
        .values(
            status=TestRunStatus.ERROR.value,
            completed_at=datetime.now(UTC),
            error_message="Test abandoned (timeout or worker crash)"
        )
    )
    await db.commit()

# Run this every 5 minutes via background task or cron
```

---

### 2. No Per-Scenario Timeout (Tests Can Run Indefinitely)

**Location:** `test_runner.py:202-209`, `test_caller.py:155`

**Problem:**
```python
# test_runner.py
start_time = time.monotonic()
conversation = await self._simulate_conversation(agent=agent, scenario=scenario)
evaluation = await self._evaluate_conversation(agent, scenario, conversation)
# ↑ No timeout wrapper around these calls
```

```python
# test_caller.py
while self.turn_count < self.max_turns:
    agent_response = await self._get_agent_response()  # ← Can hang forever
    # ...
```

**What Can Go Wrong:**
- Claude API hangs (rare but possible despite timeouts)
- Network partition → retry + circuit breaker delays up to ~90s per call
- Scenario with 20 turns × 3 API calls per turn × 90s = **54 minutes per test**
- `run_all_scenarios()` with 10 scenarios = **9 hours**

**Impact:**
- User initiates "Run All Tests" → goes to lunch → tests still running
- Background tasks consume worker slots indefinitely
- FastAPI BackgroundTasks has no timeout mechanism
- No way to cancel long-running tests

**Recommendation:**
```python
import asyncio
from contextlib import asynccontextmanager

TEST_RUN_TIMEOUT = 300  # 5 minutes per scenario

async def run_scenario(...) -> TestRun:
    try:
        async with asyncio.timeout(TEST_RUN_TIMEOUT):
            # ... existing code ...
    except asyncio.TimeoutError:
        log.warning("test_run_timeout", test_run_id=str(test_run.id))
        test_run.status = TestRunStatus.ERROR.value
        test_run.error_message = f"Test exceeded {TEST_RUN_TIMEOUT}s timeout"
        test_run.completed_at = datetime.now(UTC)
        await self.db.commit()
```

---

### 3. Circuit Breaker Opens → All Future Tests Fail Immediately

**Location:** `resilience.py:116-122`, `test_runner.py:419-430`

**Problem:**
```python
# resilience.py
if claude_circuit_breaker.current_state == "open":
    logger.error("claude_circuit_open", ...)
    raise CircuitBreakerError(claude_circuit_breaker)
```

```python
# test_runner.py - run_all_scenarios()
for scenario in scenarios:
    try:
        test_run = await self.run_scenario(...)  # ← Raises CircuitBreakerError
        results.append(test_run)
    except Exception:
        log.exception("scenario_failed", scenario_id=str(scenario.id))
        # ← Exception swallowed, no TestRun created
```

**What Can Go Wrong:**
1. User runs 10 scenarios
2. First 5 pass
3. Claude API has transient issues → circuit opens after 5 failures
4. Next 5 scenarios all fail instantly with `CircuitBreakerError`
5. No TestRun records created for failed scenarios
6. User sees only 5/10 tests in UI, thinks tests didn't run

**Impact:**
- **Data loss:** No record of attempted tests when circuit opens
- User confusion: "I clicked run 10 tests, only 5 show up"
- Can't distinguish between "not run" and "circuit open"
- No retry mechanism after circuit recovers

**Recommendation:**
```python
for scenario in scenarios:
    try:
        if is_circuit_open():
            # Create ERROR test run immediately
            test_run = TestRun(
                scenario_id=scenario.id,
                agent_id=agent_id,
                user_id=user_id,
                status=TestRunStatus.ERROR.value,
                error_message="Claude API circuit breaker open",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            self.db.add(test_run)
            await self.db.commit()
            results.append(test_run)
            continue  # Skip to next scenario

        test_run = await self.run_scenario(...)
        results.append(test_run)
    except CircuitBreakerError:
        # Same handling as above
        ...
```

---

### 4. No Conversation State Recovery After Partial API Failure

**Location:** `test_runner.py:254-308`, `test_caller.py:138-211`

**Problem:**
```python
# test_runner.py
for turn in scenario.conversation_flow:
    if turn["speaker"] == "user":
        user_message = turn["message"]
        messages.append({"role": "user", "content": user_message})
        conversation.append({...})

        # ↓ If this fails on turn 8 of 15...
        response = await call_claude_with_resilience(...)

        agent_response = response.content[0].text
        messages.append({"role": "assistant", "content": agent_response})
        conversation.append({...})
        # ↑ ...all 7 previous turns are lost

return conversation  # ← Empty if exception raised
```

**What Can Go Wrong:**
- Test runs for 8 turns (4 API calls)
- Turn 9 fails with `APIError` (non-retryable)
- Exception propagates → `conversation` list lost
- Test result saved with `actual_transcript: []` (empty)
- No partial results saved

**Impact:**
- Cannot debug failures (no transcript)
- Cannot see how far test progressed
- Wasted API costs (successful turns not recorded)
- Poor UX: "Test failed" with no details

**Recommendation:**
```python
# test_caller.py
async def execute_scenario(self) -> TestResult:
    try:
        # ... main loop ...
    except Exception as e:
        # ↓ Return partial results instead of empty
        return TestResult(
            scenario_id=str(self.scenario.id),
            scenario_name=self.scenario.name,
            passed=False,
            completion_reason="error",
            conversation=self.conversation,  # ← Preserve partial conversation
            turn_count=self.turn_count,
            issues_found=[f"Failed at turn {self.turn_count}: {str(e)}"],
            duration_ms=int((time.monotonic() - start_time) * 1000),
            notes=f"Partial execution: {self.turn_count}/{self.max_turns} turns"
        )
```

---

### 5. Invalid Scenario Data Crashes Test Runner

**Location:** `test_runner.py:276-306`, `test_caller.py:213-220`

**Problem:**
```python
# test_runner.py
for turn in scenario.conversation_flow:
    if turn["speaker"] == "user":  # ← KeyError if "speaker" missing
        user_message = turn["message"]  # ← KeyError if "message" missing
```

```python
# test_caller.py
def _get_initial_message(self) -> str | None:
    if self.scenario.conversation_flow:
        for turn in self.scenario.conversation_flow:
            if turn.get("speaker") == "user":
                msg = turn.get("message", "")  # ← OK
                return str(msg) if msg else None
    return None
```

**What Can Go Wrong:**
- User creates custom scenario via API with invalid `conversation_flow`
- Built-in scenarios corrupted (typo during seeding)
- Example bad data:
  ```json
  {"conversation_flow": [
    {"role": "user", "text": "Hello"},  // "speaker" typo, "message" typo
    {"speaker": "agent", "message": null}  // null message
  ]}
  ```
- Test runner crashes with `KeyError: 'speaker'`
- Test stuck in RUNNING state (see Issue #1)

**Impact:**
- One bad scenario crashes entire test suite
- No validation at scenario creation time
- User sees "ERROR" with cryptic traceback

**Recommendation:**
```python
# Add validation in testing.py create_scenario endpoint
@router.post("/scenarios", ...)
async def create_scenario(request: TestScenarioCreate, ...):
    # Validate conversation_flow structure
    for i, turn in enumerate(request.conversation_flow):
        if not isinstance(turn, dict):
            raise HTTPException(400, f"conversation_flow[{i}] must be object")
        if "speaker" not in turn or turn["speaker"] not in ["user", "agent"]:
            raise HTTPException(400, f"conversation_flow[{i}] missing valid 'speaker'")
        if "message" not in turn or not isinstance(turn["message"], str):
            raise HTTPException(400, f"conversation_flow[{i}] missing string 'message'")

    # ... create scenario ...
```

---

### 6. Agent Without System Prompt Causes Silent Failures

**Location:** `test_runner.py:290-296`, `test_caller.py:236-242`

**Problem:**
```python
# test_runner.py
response = await call_claude_with_resilience(
    client=client,
    model=settings.QA_EVALUATION_MODEL,
    max_tokens=500,
    messages=messages,
    system=agent.system_prompt,  # ← What if agent.system_prompt is None?
)
```

```python
# resilience.py
async def call_claude_with_resilience(..., system: str | None = None):
    if system:
        return await client.messages.create(..., system=system)
    return await client.messages.create(...)  # ← OK, handles None
```

**What Can Go Wrong:**
- Agent created with `system_prompt = None` or empty string
- Test runs but agent has no instructions
- Agent produces garbage responses
- Test fails but user doesn't know why
- No warning logged about missing prompt

**Impact:**
- Tests pass/fail inconsistently
- Hard to debug (looks like agent issue, not config issue)
- Wastes time investigating wrong problem

**Recommendation:**
```python
# test_runner.py - run_scenario()
if not agent.system_prompt or not agent.system_prompt.strip():
    raise ValueError(
        f"Agent {agent.id} has no system_prompt. "
        "Cannot run test without agent instructions."
    )

# Also validate in agent creation API
@router.post("/api/v1/agents")
async def create_agent(...):
    if not request.system_prompt or not request.system_prompt.strip():
        raise HTTPException(400, "system_prompt is required")
```

---

### 7. No Concurrency Limit (Thundering Herd Problem)

**Location:** `testing.py:600-632`, `test_runner.py:388-431`

**Problem:**
```python
# testing.py
@router.post("/run-all")
async def run_all_tests(...):
    # ... count scenarios ...

    # ↓ No concurrency limit
    background_tasks.add_task(
        _run_all_scenarios_background,
        agent_id=agent_uuid,
        ...
    )
```

```python
# _run_all_scenarios_background()
async with AsyncSessionLocal() as db:
    runner = TestRunner(db)
    results = await runner.run_all_scenarios(...)  # ← Sequential
```

```python
# test_runner.py - run_all_scenarios()
for scenario in scenarios:  # ← Sequential loop
    test_run = await self.run_scenario(...)
    results.append(test_run)
```

**What Can Go Wrong:**
1. User clicks "Run All Tests" 3 times (UI doesn't disable button)
2. 3 background tasks start
3. Each runs 10 scenarios sequentially
4. 30 scenarios × 2 min each = **60 minutes of total work**
5. All 3 tasks hit Claude API simultaneously
6. Rate limits exceeded → more retries → even slower
7. Database connections exhausted (3 long-lived sessions)

**Impact:**
- No deduplication (same test runs multiple times)
- Resource exhaustion (DB connections, API rate limits)
- Tests take 3x longer than necessary
- Poor concurrency model (sequential instead of parallel with limit)

**Recommendation:**
```python
# Add Redis-based lock for deduplication
from app.db.redis import get_redis

@router.post("/run-all")
async def run_all_tests(...):
    redis = await get_redis()
    lock_key = f"test_run_lock:{agent_uuid}"

    # Try to acquire lock (expires in 30 min)
    if not await redis.set(lock_key, "1", nx=True, ex=1800):
        raise HTTPException(409, "Tests already running for this agent")

    background_tasks.add_task(_run_all_scenarios_background, ...)

# Add concurrency limit in run_all_scenarios()
import asyncio

async def run_all_scenarios(...) -> list[TestRun]:
    semaphore = asyncio.Semaphore(3)  # Max 3 concurrent tests

    async def run_with_limit(scenario):
        async with semaphore:
            return await self.run_scenario(...)

    tasks = [run_with_limit(s) for s in scenarios]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Handle exceptions in results
    return [r for r in results if isinstance(r, TestRun)]
```

---

### 8. Evaluation Failure Loses Conversation Data

**Location:** `test_runner.py:210-234`

**Problem:**
```python
start_time = time.monotonic()

conversation = await self._simulate_conversation(agent, scenario)
evaluation = await self._evaluate_conversation(agent, scenario, conversation)
# ↑ If evaluation fails, conversation is lost

duration_ms = int((time.monotonic() - start_time) * 1000)

# ↓ Only updates if evaluation succeeds
test_run.status = TestRunStatus.PASSED.value if evaluation["passed"] else TestRunStatus.FAILED.value
test_run.actual_transcript = conversation
test_run.behavior_matches = evaluation.get("behavior_matches")
# ...

await self.db.commit()
```

**What Can Go Wrong:**
- Conversation simulation succeeds (15 API calls, $0.30 spent)
- Evaluation call fails (network issue, timeout, JSON parse error)
- Exception handler sets `status=ERROR` but **doesn't save conversation**
- User sees "Test failed" with no transcript

**Impact:**
- Lost $0.30 worth of API calls
- Cannot debug why test failed
- Must re-run entire test (another $0.30)
- Poor UX (no partial results)

**Recommendation:**
```python
try:
    conversation = await self._simulate_conversation(agent, scenario)

    # ↓ Save conversation BEFORE evaluation
    test_run.actual_transcript = conversation
    await self.db.commit()

    evaluation = await self._evaluate_conversation(agent, scenario, conversation)

    # Update with evaluation results
    test_run.status = TestRunStatus.PASSED.value if evaluation["passed"] else TestRunStatus.FAILED.value
    test_run.behavior_matches = evaluation.get("behavior_matches")
    # ...
    await self.db.commit()

except Exception as e:
    # Conversation already saved, just mark as error
    test_run.status = TestRunStatus.ERROR.value
    test_run.error_message = str(e)
    await self.db.commit()
```

---

## HIGH SEVERITY ISSUES (Fail Under Load)

### 9. JSON Parsing Fallback Returns Misleading "Pass"

**Location:** `test_runner.py:360-386`

**Problem:**
```python
# Parse JSON response
try:
    result = json.loads(response_text)
    if isinstance(result, dict):
        return cast("dict[str, Any]", result)
except json.JSONDecodeError:
    pass

# Try to extract JSON from markdown
json_match = re.search(r"\{[\s\S]*\}", response_text)
if json_match:
    try:
        result = json.loads(json_match.group(0))
        if isinstance(result, dict):
            return cast("dict[str, Any]", result)
    except json.JSONDecodeError:
        pass

# Default response if parsing fails
return {
    "overall_score": 50,  # ← Arbitrary score
    "passed": False,
    "behavior_matches": {},
    "criteria_results": {},
    "issues_found": ["Failed to parse evaluation response"],
    "recommendations": ["Re-run the test"],
}
```

**What Can Go Wrong:**
- Claude returns valid response but in unexpected format
- Example: `"Here's my evaluation:\n\n{...json...}\n\nHope this helps!"`
- Regex extracts `{...json...}` but it's incomplete (e.g., truncated)
- `json.loads()` raises `JSONDecodeError`
- Fallback returns `overall_score=50`, `passed=False`
- **User sees "Test FAILED with score 50"** when evaluation was actually positive

**Impact:**
- Misleading results (false negatives)
- User loses trust in testing framework
- Hard to debug (no indication of parse failure in UI)

**Recommendation:**
```python
# Log parse failures clearly
except json.JSONDecodeError as e:
    log.error(
        "evaluation_parse_failed",
        response_text=response_text[:500],  # Log snippet
        error=str(e),
    )
    return {
        "overall_score": 0,  # ← Fail clearly, not 50
        "passed": False,
        "behavior_matches": {},
        "criteria_results": {},
        "issues_found": [
            "Failed to parse evaluation response",
            f"Parse error: {str(e)}",
            "Raw response logged for debugging"
        ],
        "recommendations": ["Check evaluation prompt format", "Re-run test"],
    }
```

---

### 10. No Rate Limit Backpressure

**Location:** `test_runner.py:419-430`, `resilience.py:59-77`

**Problem:**
```python
# test_runner.py
for scenario in scenarios:  # ← No delay between scenarios
    test_run = await self.run_scenario(...)
    results.append(test_run)
```

```python
# resilience.py - retry decorator
return retry(
    stop=stop_after_attempt(settings.ANTHROPIC_MAX_RETRIES),
    wait=wait_exponential(multiplier=..., max=30),
    retry=retry_if_exception_type((
        anthropic.RateLimitError,  # ← Retries rate limits
        ...
    ))
)
```

**What Can Go Wrong:**
1. User runs 20 scenarios back-to-back
2. Each scenario makes 10 API calls (200 total)
3. Claude API rate limit: 50 RPM (requests per minute)
4. After 50 requests, all future requests return 429 RateLimitError
5. Retry logic kicks in: wait 2s, 4s, 8s, 16s, 30s (60s total)
6. 150 requests × 60s retry delay = **2.5 hours of wasted waiting**

**Impact:**
- Tests take forever when rate limited
- Circuit breaker may open (5 consecutive rate limit errors)
- Poor resource utilization (threads blocked on sleep)
- User abandons test suite

**Recommendation:**
```python
# Add rate limiting at test runner level
import asyncio
from asyncio import Semaphore

class TestRunner:
    def __init__(self, db: AsyncSession):
        self.rate_limiter = Semaphore(10)  # Max 10 concurrent API calls
        self.last_request_time = 0.0
        self.min_request_interval = 0.1  # 100ms between requests

    async def _rate_limited_call(self, call):
        """Wrap API calls with rate limiting."""
        async with self.rate_limiter:
            # Ensure minimum interval between requests
            now = time.monotonic()
            elapsed = now - self.last_request_time
            if elapsed < self.min_request_interval:
                await asyncio.sleep(self.min_request_interval - elapsed)

            result = await call()
            self.last_request_time = time.monotonic()
            return result
```

---

### 11. Database Session Leaks in Background Tasks

**Location:** `testing.py:600-632`

**Problem:**
```python
async def _run_all_scenarios_background(...) -> None:
    try:
        async with AsyncSessionLocal() as db:
            runner = TestRunner(db)
            results = await runner.run_all_scenarios(...)  # ← Can take 30 min
            # ... logging ...
    except Exception:
        log.exception("background_test_run_failed")
        # ↑ Exception handler doesn't close DB session explicitly
```

**What Can Go Wrong:**
- Background task starts with DB session
- Test runs for 20 minutes
- Exception raised (e.g., circuit breaker)
- `async with` context manager should close session
- BUT: If FastAPI process crashes, session not closed
- Database connection held open indefinitely
- Repeat 10 times → 10 leaked connections
- PostgreSQL max_connections = 100 → server runs out of connections

**Impact:**
- Database connection pool exhaustion
- Other requests fail with "too many connections" error
- Requires DBA intervention to kill connections
- Cascading failure (entire app becomes unavailable)

**Recommendation:**
```python
# Add explicit session timeout
async def _run_all_scenarios_background(...):
    db_session = None
    try:
        db_session = AsyncSessionLocal()
        runner = TestRunner(db_session)

        # Use asyncio.timeout for the entire background task
        async with asyncio.timeout(1800):  # 30 min max
            results = await runner.run_all_scenarios(...)

    except asyncio.TimeoutError:
        log.error("background_test_timeout")
    except Exception:
        log.exception("background_test_failed")
    finally:
        if db_session:
            await db_session.close()
            log.debug("db_session_closed")
```

---

### 12. Circuit Breaker State Shared Across All Tests

**Location:** `resilience.py:25-32`

**Problem:**
```python
# Global circuit breaker instance
claude_circuit_breaker = CircuitBreaker(
    fail_max=settings.ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD,
    timeout_duration=settings.ANTHROPIC_CIRCUIT_RECOVERY_TIMEOUT,
    name="claude_api",
)
```

**What Can Go Wrong:**
- User A runs tests for Agent A → 5 failures → circuit opens
- User B tries to run tests for Agent B → immediately fails (circuit open)
- User C tries to run tests for Agent C → also fails
- All users blocked for 60 seconds
- User B and C didn't cause the failures but are punished

**Impact:**
- Multi-tenant isolation broken
- One user's failures affect all users
- Poor UX (unexpected errors)
- No per-user or per-agent circuit breaker

**Recommendation:**
```python
# Option 1: Per-user circuit breakers
circuit_breakers: dict[int, CircuitBreaker] = {}

def get_circuit_breaker(user_id: int) -> CircuitBreaker:
    if user_id not in circuit_breakers:
        circuit_breakers[user_id] = CircuitBreaker(
            fail_max=5,
            timeout_duration=60,
            name=f"claude_api_user_{user_id}",
        )
    return circuit_breakers[user_id]

# Option 2: Remove circuit breaker entirely
# Claude API has its own rate limiting and auto-scales
# Circuit breaker may be premature optimization
```

---

### 13. No Idempotency on Test Runs

**Location:** `testing.py:535-597`

**Problem:**
```python
@router.post("/run")
async def run_test(request: RunTestRequest, ...):
    # ... validate scenario and agent ...

    # ↓ Always creates new test run
    runner = TestRunner(db)
    test_run = await runner.run_scenario(...)

    return RunTestResponse(
        test_run_id=str(test_run.id),
        ...
    )
```

**What Can Go Wrong:**
- User clicks "Run Test" button
- Network hiccup → request times out
- User clicks again (retry)
- Backend receives both requests
- Two identical test runs created
- Both run simultaneously (wasted API costs)

**Impact:**
- Duplicate test runs in database
- Wasted API costs (2x)
- Confusing UI (two results for same test)
- No deduplication

**Recommendation:**
```python
@router.post("/run")
async def run_test(request: RunTestRequest, ...):
    # Check for recent duplicate runs (within 5 min)
    recent_cutoff = datetime.now(UTC) - timedelta(minutes=5)
    result = await db.execute(
        select(TestRun).where(
            TestRun.scenario_id == scenario_uuid,
            TestRun.agent_id == agent_uuid,
            TestRun.user_id == current_user.id,
            TestRun.created_at > recent_cutoff,
            TestRun.status.in_([
                TestRunStatus.PENDING.value,
                TestRunStatus.RUNNING.value
            ])
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        return RunTestResponse(
            message="Test already running",
            test_run_id=str(existing.id),
            status=existing.status,
        )

    # ... create new test run ...
```

---

### 14. Max Turns Limit Too Low for Complex Scenarios

**Location:** `test_caller.py:117`

**Problem:**
```python
self.max_turns = 20  # Default max turns
```

**What Can Go Wrong:**
- Complex scenario (e.g., "Handle Angry Customer with Escalation")
- Requires 25+ turns to complete (realistic for voice conversations)
- Test hits turn limit → marked as "timeout" failure
- Scenario wasn't tested completely
- False negative (scenario might have passed if allowed to continue)

**Impact:**
- Complex scenarios always fail
- Can't test long conversations
- Hardcoded limit (not configurable)

**Recommendation:**
```python
# Add max_turns to TestScenario model
class TestScenario(Base):
    # ...
    max_turns: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

# Use scenario-specific limit
class AITestCaller:
    def __init__(self, scenario: TestScenario, agent: Agent):
        self.max_turns = scenario.max_turns or 20
```

---

## MEDIUM SEVERITY ISSUES (UX/Monitoring)

### 15. No Progress Reporting for Long-Running Tests

**Location:** `testing.py:634-700`

**Problem:**
```python
@router.post("/run-all")
async def run_all_tests(...):
    # Queue background task
    background_tasks.add_task(_run_all_scenarios_background, ...)

    return RunAllTestsResponse(
        message=f"Queued {scenario_count} tests",
        test_count=scenario_count,
        queued=True,  # ← User has no way to track progress
    )
```

**What Can Go Wrong:**
- User runs 20 scenarios (40 min total)
- Response returns immediately: "Queued 20 tests"
- User must poll `/runs` endpoint repeatedly to check status
- No real-time updates (progress bar, current scenario, ETA)
- User doesn't know if tests are stuck or progressing

**Impact:**
- Poor UX (no feedback loop)
- User abandons page → background task continues
- No way to cancel running tests
- User polls API aggressively → waste of resources

**Recommendation:**
```python
# Option 1: WebSocket for real-time updates
@router.websocket("/ws/test-progress/{test_suite_id}")
async def test_progress_ws(websocket: WebSocket, test_suite_id: str):
    await websocket.accept()
    while True:
        # Send progress updates
        await websocket.send_json({
            "current_scenario": "...",
            "completed": 5,
            "total": 20,
            "progress_pct": 25.0,
            "eta_seconds": 1800
        })
        await asyncio.sleep(5)

# Option 2: Server-Sent Events (SSE)
@router.get("/test-progress/{test_suite_id}")
async def test_progress_sse(test_suite_id: str):
    async def event_stream():
        while True:
            data = get_test_progress(test_suite_id)
            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(5)
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

---

### 16. No Test Cancellation Support

**Location:** `testing.py`, `test_runner.py`

**Problem:**
- User starts "Run All Tests" (20 scenarios, 40 min)
- Realizes they selected wrong agent
- **No way to cancel** running tests
- Must wait 40 minutes or restart server

**Impact:**
- Wasted API costs (can't stop mid-run)
- Wasted compute resources
- Poor UX (no cancel button)
- Forces user to wait or take drastic action

**Recommendation:**
```python
# Add cancellation support via Redis
from asyncio import CancelledError

test_cancellations = set()  # In-memory (or use Redis)

@router.post("/cancel-test-suite/{test_suite_id}")
async def cancel_test_suite(test_suite_id: str, ...):
    test_cancellations.add(test_suite_id)
    return {"message": "Cancellation requested"}

async def _run_all_scenarios_background(...):
    test_suite_id = str(uuid.uuid4())

    for scenario in scenarios:
        # Check for cancellation
        if test_suite_id in test_cancellations:
            log.info("test_suite_cancelled", test_suite_id=test_suite_id)
            test_cancellations.remove(test_suite_id)
            break

        test_run = await runner.run_scenario(...)
        results.append(test_run)
```

---

### 17. No Metrics/Monitoring for Circuit Breaker State

**Location:** `resilience.py`

**Problem:**
- Circuit breaker opens → all tests fail
- No Prometheus metrics exposed
- No alerting when circuit opens
- No visibility into circuit state in production

**Impact:**
- Ops team doesn't know Claude API is down
- No proactive monitoring
- Can't correlate test failures with circuit state
- Poor observability

**Recommendation:**
```python
from prometheus_client import Counter, Gauge

circuit_state_gauge = Gauge(
    "claude_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=half_open, 2=open)"
)
circuit_failures_counter = Counter(
    "claude_circuit_breaker_failures_total",
    "Total circuit breaker failures"
)

# Update in call_claude_with_resilience()
except anthropic.APIError as e:
    circuit_failures_counter.inc()

    # Update gauge
    state_map = {"closed": 0, "half_open": 1, "open": 2}
    state = str(claude_circuit_breaker.current_state).lower()
    circuit_state_gauge.set(state_map.get(state, 0))

    raise
```

---

### 18. Agent/Scenario Relationship Not Validated

**Location:** `testing.py:535-597`

**Problem:**
```python
# Verify agent exists and belongs to user
agent_result = await db.execute(
    select(Agent).where(
        Agent.id == agent_uuid,
        Agent.user_id == current_user.id,
    )
)
if not agent_result.scalar_one_or_none():
    raise HTTPException(status_code=404, detail="Agent not found")

# Run the test
runner = TestRunner(db)
test_run = await runner.run_scenario(...)
# ↑ No validation that agent is suitable for scenario
```

**What Can Go Wrong:**
- Scenario designed for "customer support" agents
- User runs it against "sales" agent (different tools, different prompt)
- Test fails but it's not a real failure (wrong agent type)
- User wastes time debugging

**Impact:**
- Misleading test results
- User confusion (why did this fail?)
- No guidance on agent-scenario compatibility

**Recommendation:**
```python
# Add agent requirements to scenario
class TestScenario(Base):
    # ...
    required_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    agent_category: Mapped[str | None] = mapped_column(String(50), nullable=True)

# Validate in run_test endpoint
@router.post("/run")
async def run_test(...):
    # ... get agent and scenario ...

    if scenario.required_tools:
        agent_tools = set(agent.enabled_tool_ids or [])
        required = set(scenario.required_tools)
        missing = required - agent_tools
        if missing:
            raise HTTPException(
                400,
                f"Agent missing required tools: {', '.join(missing)}"
            )

    # ... run test ...
```

---

## Summary of Recommendations

### Immediate Fixes (Pre-Production)
1. Add abandoned test cleanup job (Issue #1)
2. Add per-test timeout (Issue #2)
3. Handle circuit breaker state in run_all_scenarios (Issue #3)
4. Save conversation before evaluation (Issue #8)
5. Add concurrency limit + deduplication (Issue #7)

### High Priority (First Month)
6. Preserve partial results on failure (Issue #4)
7. Validate scenario data structure (Issue #5)
8. Add rate limiting backpressure (Issue #10)
9. Fix session leak in background tasks (Issue #11)
10. Add test cancellation support (Issue #16)

### Nice to Have (Second Month)
11. Add progress reporting (WebSocket/SSE) (Issue #15)
12. Add Prometheus metrics (Issue #17)
13. Improve JSON parsing error handling (Issue #9)
14. Make max_turns configurable (Issue #14)
15. Add agent-scenario compatibility validation (Issue #18)

### Consider Removing
- Circuit breaker (Issue #12) - may be premature optimization; Claude API has own rate limiting

---

## Severity Scoring

| Issue | Severity | Impact | Likelihood | Risk Score |
|-------|----------|--------|------------|------------|
| #1 Stuck RUNNING state | CRITICAL | High | High | 9/10 |
| #2 No timeout | CRITICAL | High | Medium | 8/10 |
| #3 Circuit breaker data loss | CRITICAL | High | Medium | 8/10 |
| #4 No state recovery | CRITICAL | High | Medium | 8/10 |
| #5 Invalid scenario crash | CRITICAL | High | Medium | 7/10 |
| #6 Missing system prompt | CRITICAL | Medium | Low | 5/10 |
| #7 No concurrency limit | CRITICAL | High | High | 9/10 |
| #8 Evaluation failure data loss | CRITICAL | High | Medium | 8/10 |
| #9 Misleading JSON fallback | HIGH | Medium | Medium | 6/10 |
| #10 No rate limit backpressure | HIGH | High | High | 8/10 |
| #11 Session leaks | HIGH | High | Medium | 7/10 |
| #12 Global circuit breaker | HIGH | Medium | High | 7/10 |
| #13 No idempotency | HIGH | Low | High | 5/10 |
| #14 Max turns too low | HIGH | Low | Medium | 4/10 |
| #15 No progress reporting | MEDIUM | Low | High | 4/10 |
| #16 No cancellation | MEDIUM | Medium | Medium | 5/10 |
| #17 No monitoring | MEDIUM | Medium | High | 6/10 |
| #18 No compatibility check | MEDIUM | Low | Medium | 3/10 |

**Average Risk Score: 6.5/10**
**Production Readiness: 60%** (needs critical fixes before launch)

---

## Conclusion

The test runner has **solid foundations** (good resilience patterns, proper error handling, structured logging) but needs **8 critical fixes** before production deployment. The most dangerous issues are:

1. Test runs getting stuck in RUNNING state (no cleanup)
2. No overall timeout (tests can run indefinitely)
3. Data loss when circuit breaker opens
4. No concurrency limits (thundering herd)

With these fixes, the system would be production-ready for moderate load. For high-scale deployment (100+ concurrent users, 1000+ tests/day), additional improvements are needed (progress reporting, cancellation, better rate limiting).

**Estimated Effort:**
- Critical fixes: 2-3 days
- High priority fixes: 3-4 days
- Nice to have: 5-7 days
- **Total: 10-14 days to production-ready**
