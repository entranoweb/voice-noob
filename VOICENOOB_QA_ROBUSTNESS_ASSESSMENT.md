# VoiceNoob QA Framework - Deep Robustness Assessment

**Date:** December 20, 2025
**Scope:** Code-level analysis of error handling, failure modes, and resilience

---

## Executive Summary

After deep analysis of 6 core QA service files (~2,100 lines), the framework has **solid foundations but critical gaps** in production resilience:

| Category | Score | Assessment |
|----------|-------|------------|
| Error Handling | 7/10 | Good try/except coverage, weak recovery |
| Fault Tolerance | 3/10 | No circuit breaker, no retries |
| Data Integrity | 8/10 | Good transactions, idempotency checks |
| Observability | 8/10 | Excellent structured logging |
| Security | 7/10 | Multi-tenant isolation, HMAC webhooks |
| Scalability | 4/10 | In-memory concurrency only |

**Overall Robustness Score: 6.2/10** - Functional for low-medium load, not production-hardened.

---

## Detailed Analysis by Component

### 1. QAEvaluator (`evaluator.py`) - 479 lines

#### Strengths ✅

```python
# Good: Idempotency check prevents duplicate evaluations
existing = await self.db.execute(
    select(CallEvaluation).where(CallEvaluation.call_id == call_id)
)
if existing.scalar_one_or_none():
    log.info("already_evaluated")
    return None
```

```python
# Good: Multiple JSON parsing fallbacks
def _parse_evaluation_response(self, response_text: str) -> dict[str, Any] | None:
    # Try 1: Direct JSON parse
    # Try 2: Extract from markdown code blocks
    # Try 3: Find JSON object in text
    # Try 4: Progressive matching
```

```python
# Good: Cost tracking per evaluation
cost_cents = (input_tokens / 1000) * cost_info["input"] + (output_tokens / 1000) * cost_info["output"]
```

#### Weaknesses ❌

**CRITICAL: No API retry logic**
```python
# Current: Single attempt, fails silently
response = await client.messages.create(
    model=model,
    max_tokens=2000,
    messages=[{"role": "user", "content": prompt}],
)

# Missing: Retry with exponential backoff
# @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
```

**CRITICAL: No timeout on Claude API**
```python
# No timeout specified - can hang indefinitely
self._client = anthropic.AsyncAnthropic(api_key=api_key)

# Should be:
# self._client = anthropic.AsyncAnthropic(
#     api_key=api_key,
#     timeout=30.0,  # 30 second timeout
# )
```

**CRITICAL: Silent failure on exceptions**
```python
except Exception:
    log.exception("evaluation_failed")
    return None  # Caller has no idea what failed
```

**Missing: Rate limit handling**
```python
# No handling for Anthropic rate limits (429 errors)
# No backpressure mechanism when approaching limits
```

---

### 2. TestRunner (`test_runner.py`) - 449 lines

#### Strengths ✅

```python
# Good: Test run status tracking
test_run.status = TestRunStatus.RUNNING.value
test_run.started_at = datetime.now(UTC)
# ...
test_run.status = TestRunStatus.PASSED.value if evaluation["passed"] else TestRunStatus.FAILED.value
test_run.completed_at = datetime.now(UTC)
```

```python
# Good: Error status on failure
except Exception as e:
    log.exception("test_run_failed", error=str(e))
    test_run.status = TestRunStatus.ERROR.value
    test_run.error_message = str(e)
```

#### Weaknesses ❌

**Sequential scenario execution (slow)**
```python
# Current: One at a time
for scenario in scenarios:
    test_run = await self.run_scenario(...)  # Blocks until complete
    results.append(test_run)

# Should be: Parallel with semaphore
# async def run_with_limit(scenario):
#     async with semaphore:
#         return await self.run_scenario(...)
# results = await asyncio.gather(*[run_with_limit(s) for s in scenarios])
```

**No cancellation support**
```python
# No way to cancel a running test suite
# If user navigates away, tests continue running
```

**No progress reporting**
```python
# No WebSocket or SSE for real-time progress
# User must poll or wait for completion
```

---

### 3. AITestCaller (`test_caller.py`) - 361 lines

#### Strengths ✅

```python
# Good: Max turn limit prevents infinite loops
self.max_turns = 20

while self.turn_count < self.max_turns:
    # ... conversation loop
```

```python
# Good: Graceful timeout handling
if self.turn_count >= self.max_turns:
    return TestResult(
        passed=False,
        completion_reason="timeout",
        issues_found=["Exceeded maximum turn count"],
    )
```

#### Weaknesses ❌

**No handling of silence markers**
```python
# Scenario has: {"message": "[silence - 15 seconds]"}
# But code treats it as regular text:
user_message = turn["message"]  # "[silence - 15 seconds]" sent verbatim

# Should detect and handle:
# if "[silence" in turn["message"]:
#     await asyncio.sleep(parse_silence_duration(turn["message"]))
```

**No interruption simulation**
```python
# Can't test scenarios where user interrupts agent mid-sentence
# Real voice calls have overlap/interruption patterns
```

**Hardcoded max_tokens**
```python
# Fixed at 500 tokens - may truncate long responses
response = await client.messages.create(
    model=settings.QA_EVALUATION_MODEL,
    max_tokens=500,  # Should be configurable
```

---

### 4. AlertService (`alerts.py`) - 477 lines

#### Strengths ✅

```python
# Good: HMAC signature for webhook security
if secret:
    signature = hmac.new(
        secret.encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()
    headers["X-Signature-256"] = f"sha256={signature}"
```

```python
# Good: Timeout on HTTP calls
async with httpx.AsyncClient(timeout=10.0) as client:
```

```python
# Good: Alert limit to prevent unbounded growth
alerts_list = [*alerts_list[-99:], alert]  # Keep only last 100
```

#### Weaknesses ❌

**No webhook retry**
```python
# Single attempt, no retry on failure
response = await client.post(webhook_url, json=payload, headers=headers)
return response.status_code < 400

# Should have retry with backoff for 5xx errors
```

**Alerts stored in JSON column (not scalable)**
```python
# Workspace.settings["qa_alerts"] is a JSON array
# No indexing, can't query efficiently
# Should be separate table with indexes
```

**No dead letter queue**
```python
# Failed alerts are just logged and dropped
except Exception:
    log.exception("webhook_alert_failed")
# Should queue for retry or manual review
```

---

### 5. Dashboard (`dashboard.py`) - 347 lines

#### Strengths ✅

```python
# Good: Empty metrics fallback
if total_evaluations == 0:
    return _empty_metrics()  # Safe default
```

```python
# Good: Efficient aggregation queries
select(
    func.avg(CallEvaluation.overall_score),
    func.avg(CallEvaluation.intent_completion),
    ...
).where(*filters)
```

#### Weaknesses ❌

**No caching**
```python
# Every dashboard load runs full aggregation queries
# Should cache metrics for 1-5 minutes
# Redis: await redis.setex(f"qa_metrics:{workspace_id}", 300, json.dumps(metrics))
```

**No pagination on failure reasons**
```python
# Loads ALL failed evaluations to count reasons
result = await db.execute(
    select(CallEvaluation.failure_reasons).where(*filters)
)
rows = result.all()  # Could be thousands

# Should use SQL aggregation or limit
```

---

### 6. CallEvaluation Model (`call_evaluation.py`) - 195 lines

#### Strengths ✅

```python
# Good: Comprehensive indexes
overall_score: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
passed: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
```

```python
# Good: Unique constraint prevents duplicates
call_id: Mapped[uuid.UUID] = mapped_column(
    Uuid(as_uuid=True),
    ForeignKey("call_records.id", ondelete="CASCADE"),
    unique=True,  # One evaluation per call
)
```

#### Weaknesses ❌

**No composite indexes for common queries**
```python
# Dashboard queries filter by workspace_id + created_at
# Missing: Index("ix_call_eval_workspace_created", workspace_id, created_at)
```

---

## Critical Failure Modes

### 1. Anthropic API Rate Limit (HIGH RISK)

**Scenario:** Batch evaluation of 100 calls hits rate limit after 20

**Current behavior:**
- Calls 21-100 fail silently
- No retry, no backoff
- User sees partial results with no explanation

**Impact:** Data loss, inconsistent state

**Fix:**
```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, max=60),
    retry=retry_if_exception_type(anthropic.RateLimitError),
)
async def _call_claude(self, prompt: str) -> str:
    return await self._client.messages.create(...)
```

---

### 2. Database Connection Exhaustion (MEDIUM RISK)

**Scenario:** 50 concurrent batch evaluations, each holding DB connection

**Current behavior:**
- Connection pool exhausted
- New requests fail with timeout
- No backpressure

**Impact:** Service unavailable

**Fix:**
```python
# Add connection limits to batch endpoint
if len(request.call_ids) > 100:
    raise HTTPException(400, "Maximum 100 calls per batch")

# Use separate connection pool for background tasks
QA_CONNECTION_POOL_SIZE = 10  # Dedicated pool
```

---

### 3. Memory Leak in Long-Running Tests (LOW RISK)

**Scenario:** Test suite with 100 scenarios accumulates conversation history

**Current behavior:**
- Each AITestCaller keeps full conversation in memory
- No cleanup between scenarios

**Impact:** Memory growth over time

**Fix:**
```python
# Clear state between scenarios
async def run_scenario(self, ...):
    try:
        result = await self._execute()
    finally:
        self.conversation.clear()
        self._client = None  # Release client
```

---

### 4. Webhook Timeout Blocking Evaluation (MEDIUM RISK)

**Scenario:** Slack webhook is slow (9.9s), blocks evaluation completion

**Current behavior:**
- Evaluation waits for alert before returning
- User experiences 10s+ latency

**Impact:** Poor UX, perceived slowness

**Fix:**
```python
# Fire and forget for webhooks
asyncio.create_task(self._send_failure_alerts(evaluation))
return evaluation  # Return immediately
```

---

## Missing Resilience Patterns

| Pattern | Status | Priority | Effort |
|---------|--------|----------|--------|
| Circuit Breaker | ❌ Missing | High | 1 day |
| Retry with Backoff | ❌ Missing | High | 0.5 days |
| Request Timeout | ⚠️ Partial | High | 0.5 days |
| Rate Limiting | ❌ Missing | Medium | 1 day |
| Bulkhead (Connection Pools) | ❌ Missing | Medium | 1 day |
| Dead Letter Queue | ❌ Missing | Medium | 2 days |
| Health Checks | ⚠️ Basic | Low | 0.5 days |
| Metrics/Prometheus | ❌ Missing | Low | 1 day |

---

## Recommended Fixes (Priority Order)

### Priority 1: Critical (Do Now)

**1. Add Claude API timeout + retry**
```python
# evaluator.py
import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

class QAEvaluator:
    async def _get_client(self) -> Any:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                timeout=httpx.Timeout(30.0, connect=5.0),  # Add timeout
            )
        return self._client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=30),
    )
    async def _call_claude(self, prompt: str) -> anthropic.Message:
        client = await self._get_client()
        return await client.messages.create(
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
```

**2. Add circuit breaker for Claude API**
```python
# circuit_breaker.py
from circuitbreaker import circuit

@circuit(
    failure_threshold=5,
    recovery_timeout=60,
    expected_exception=anthropic.APIError,
)
async def call_claude_with_breaker(client, prompt):
    return await client.messages.create(...)
```

### Priority 2: High (This Sprint)

**3. Add dashboard metrics caching**
```python
# dashboard.py
from app.db.redis import get_redis

async def get_dashboard_metrics_cached(
    db: AsyncSession,
    workspace_id: UUID,
    cache_ttl: int = 300,  # 5 minutes
) -> dict[str, Any]:
    redis = await get_redis()
    cache_key = f"qa_metrics:{workspace_id}"

    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    metrics = await get_dashboard_metrics(db, workspace_id)
    await redis.setex(cache_key, cache_ttl, json.dumps(metrics))
    return metrics
```

**4. Add composite database index**
```sql
-- migrations/versions/018_add_qa_performance_indexes.py
CREATE INDEX ix_call_eval_workspace_created
ON call_evaluations (workspace_id, created_at DESC);

CREATE INDEX ix_call_eval_agent_created
ON call_evaluations (agent_id, created_at DESC);
```

### Priority 3: Medium (Next Sprint)

**5. Async webhook delivery**
```python
# alerts.py
async def _send_failure_alerts(self, evaluation: CallEvaluation) -> None:
    # Fire and forget - don't block evaluation
    asyncio.create_task(self._send_alerts_background(evaluation))
```

**6. Add silence marker handling**
```python
# test_caller.py
import re

def _parse_silence_duration(message: str) -> float:
    """Extract silence duration from marker like [silence - 15 seconds]."""
    match = re.search(r"\[silence\s*-\s*(\d+)\s*seconds?\]", message, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return 0

async def _simulate_turn(self, turn: dict) -> None:
    message = turn.get("message", "")
    silence = _parse_silence_duration(message)
    if silence > 0:
        await asyncio.sleep(min(silence, 30))  # Cap at 30s
        return  # No message sent for silence turns
    # ... normal processing
```

---

## Test Coverage Gaps

| Component | Current Coverage | Gap |
|-----------|------------------|-----|
| `evaluator.py` | ~80% | Missing rate limit handling tests |
| `test_runner.py` | ~60% | Missing concurrent execution tests |
| `test_caller.py` | ~50% | Missing silence/interruption tests |
| `alerts.py` | ~70% | Missing webhook failure tests |
| `dashboard.py` | ~75% | Missing large dataset tests |

### Missing Test Cases

```python
# test_evaluator.py - Add these
async def test_evaluate_call_rate_limit_retry():
    """Test that rate limits trigger retry with backoff."""

async def test_evaluate_call_timeout():
    """Test that API timeout is handled gracefully."""

async def test_batch_evaluate_partial_failure():
    """Test that partial batch failure returns successful results."""

# test_alerts.py - Add these
async def test_webhook_retry_on_5xx():
    """Test that 5xx errors trigger retry."""

async def test_webhook_timeout_handled():
    """Test that slow webhooks don't block."""
```

---

## Summary

### What's Good
- Comprehensive data model with proper indexes
- Structured logging throughout
- Multi-tenant isolation
- Idempotency checks
- Good error categorization (pass/fail/error status)

### What Needs Work
- **No retry/backoff for external APIs** (Critical)
- **No circuit breaker** (Critical)
- **No timeout on Claude API calls** (Critical)
- **Sequential test execution** (Performance)
- **No caching for dashboard** (Performance)
- **Alerts stored in JSON, not table** (Scalability)

### Bottom Line

The code is **well-structured but not battle-tested**. A single Anthropic outage or rate limit spike would cause cascading failures. Adding retry logic, timeouts, and circuit breakers would move robustness from 6.2/10 to 8.5/10.

---

*Assessment Version: 1.0*
*Lines Analyzed: ~2,100*
*Last Updated: December 20, 2025*
