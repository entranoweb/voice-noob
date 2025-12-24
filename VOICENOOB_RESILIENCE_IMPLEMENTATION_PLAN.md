# VoiceNoob QA - Resilience Implementation Plan

**Date:** December 21, 2025
**Scope:** Add retry, timeout, circuit breaker to Claude API calls
**Approach:** Minimal changes, maximum impact

---

## Overview

Based on [2025 best practices](https://markaicode.com/llm-api-retry-logic-implementation/), we need:
1. **Tenacity** for retry with exponential backoff
2. **aiobreaker** for async circuit breaker (FastAPI-native)
3. **httpx.Timeout** for request timeouts

**Good news:** Config already has `MAX_RETRIES=3` and `RETRY_BACKOFF_FACTOR=2.0` - just not used!

---

## Files to Modify

| File | Changes |
|------|---------|
| `backend/pyproject.toml` | Add `tenacity`, `aiobreaker` |
| `backend/app/core/config.py` | Add `ANTHROPIC_TIMEOUT` |
| `backend/app/services/qa/evaluator.py` | Add resilience to `_get_client()` and `evaluate_call()` |
| `backend/app/services/qa/test_runner.py` | Add resilience to `_get_client()` |
| `backend/app/services/qa/test_caller.py` | Add resilience to `_get_client()` |

---

## Implementation

### Step 1: Add Dependencies

```toml
# pyproject.toml - add to dependencies
tenacity = ">=8.2.0"
aiobreaker = ">=1.2.0"
```

```bash
cd backend && uv add tenacity aiobreaker
```

---

### Step 2: Add Config Setting

```python
# app/core/config.py - add after line 120

# Anthropic/Claude API
ANTHROPIC_API_KEY: str | None = None
ANTHROPIC_TIMEOUT: float = 30.0  # Claude API timeout
ANTHROPIC_MAX_RETRIES: int = 3
ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD: int = 5
ANTHROPIC_CIRCUIT_RECOVERY_TIMEOUT: int = 60
```

---

### Step 3: Create Shared Resilience Module

**New file:** `backend/app/services/qa/resilience.py`

```python
"""Resilience patterns for QA services.

Provides retry, timeout, and circuit breaker for Claude API calls.
"""

import anthropic
import httpx
import structlog
from aiobreaker import CircuitBreaker, CircuitBreakerError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings

logger = structlog.get_logger()

# Circuit breaker for Claude API
claude_circuit_breaker = CircuitBreaker(
    fail_max=settings.ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD,
    timeout_duration=settings.ANTHROPIC_CIRCUIT_RECOVERY_TIMEOUT,
    name="claude_api",
)


def get_anthropic_client() -> anthropic.AsyncAnthropic:
    """Get Anthropic client with timeout configured."""
    if not settings.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    return anthropic.AsyncAnthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=httpx.Timeout(settings.ANTHROPIC_TIMEOUT, connect=5.0),
    )


@retry(
    stop=stop_after_attempt(settings.ANTHROPIC_MAX_RETRIES),
    wait=wait_exponential(
        multiplier=settings.RETRY_BACKOFF_FACTOR,
        max=30,
    ),
    retry=retry_if_exception_type((
        anthropic.RateLimitError,
        anthropic.APIConnectionError,
        anthropic.InternalServerError,
    )),
    before_sleep=lambda retry_state: logger.warning(
        "claude_api_retry",
        attempt=retry_state.attempt_number,
        wait=retry_state.next_action.sleep,
    ),
)
async def call_claude_with_resilience(
    client: anthropic.AsyncAnthropic,
    model: str,
    max_tokens: int,
    messages: list[dict],
    system: str | None = None,
) -> anthropic.types.Message:
    """Call Claude API with retry and circuit breaker.

    Args:
        client: Anthropic client
        model: Model name
        max_tokens: Max tokens to generate
        messages: Message list
        system: Optional system prompt

    Returns:
        Claude API response

    Raises:
        CircuitBreakerError: If circuit is open
        anthropic.APIError: If all retries exhausted
    """
    if claude_circuit_breaker.current_state == "open":
        logger.error("claude_circuit_open", recovery_in=claude_circuit_breaker.timeout_duration)
        raise CircuitBreakerError(claude_circuit_breaker)

    try:
        async with claude_circuit_breaker:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system

            return await client.messages.create(**kwargs)

    except anthropic.APIError as e:
        logger.error("claude_api_error", error=str(e), status=getattr(e, "status_code", None))
        raise


def is_circuit_open() -> bool:
    """Check if circuit breaker is open."""
    return claude_circuit_breaker.current_state == "open"


def get_circuit_state() -> dict:
    """Get circuit breaker state for monitoring."""
    return {
        "state": claude_circuit_breaker.current_state,
        "fail_count": claude_circuit_breaker.fail_counter,
        "fail_max": claude_circuit_breaker.fail_max,
    }
```

---

### Step 4: Update Evaluator

**File:** `backend/app/services/qa/evaluator.py`

```python
# Replace _get_client method (lines 103-121)
from app.services.qa.resilience import (
    call_claude_with_resilience,
    get_anthropic_client,
    is_circuit_open,
)

class QAEvaluator:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.logger = logger.bind(component="qa_evaluator")
        self._client: anthropic.AsyncAnthropic | None = None

    async def _get_client(self) -> anthropic.AsyncAnthropic:
        """Get Anthropic client with timeout."""
        if self._client is None:
            self._client = get_anthropic_client()
        return self._client

    async def evaluate_call(self, call_id: uuid.UUID) -> CallEvaluation | None:
        # ... existing checks ...

        # Add circuit breaker check early
        if is_circuit_open():
            log.warning("evaluation_skipped_circuit_open")
            return None

        try:
            start_time = time.monotonic()
            prompt = EVALUATION_PROMPT_V1.format(...)

            # Replace direct API call with resilient version
            client = await self._get_client()
            response = await call_claude_with_resilience(
                client=client,
                model=settings.QA_EVALUATION_MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )

            # ... rest of method unchanged ...
```

---

### Step 5: Update Test Runner

**File:** `backend/app/services/qa/test_runner.py`

```python
# Replace _get_client and API calls

from app.services.qa.resilience import (
    call_claude_with_resilience,
    get_anthropic_client,
)

class TestRunner:
    async def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = get_anthropic_client()
        return self._client

    async def _simulate_conversation(self, agent: Agent, scenario: TestScenario):
        client = await self._get_client()
        # ...
        for turn in scenario.conversation_flow:
            if turn["speaker"] == "user":
                # Replace direct call
                response = await call_claude_with_resilience(
                    client=client,
                    model=settings.QA_EVALUATION_MODEL,
                    max_tokens=500,
                    messages=messages,
                    system=agent.system_prompt,
                )
                # ...

    async def _evaluate_conversation(self, ...):
        client = await self._get_client()
        # Replace direct call
        response = await call_claude_with_resilience(
            client=client,
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
```

---

### Step 6: Update Test Caller

**File:** `backend/app/services/qa/test_caller.py`

```python
# Same pattern - replace _get_client and all client.messages.create calls

from app.services.qa.resilience import (
    call_claude_with_resilience,
    get_anthropic_client,
)

class AITestCaller:
    async def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = get_anthropic_client()
        return self._client

    async def _get_agent_response(self) -> str:
        client = await self._get_client()
        response = await call_claude_with_resilience(
            client=client,
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=500,
            messages=messages,
            system=self.agent.system_prompt,
        )
        return str(response.content[0].text)

    async def _generate_next_message(self) -> str:
        client = await self._get_client()
        response = await call_claude_with_resilience(
            client=client,
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return str(response.content[0].text).strip()

    async def _check_completion(self) -> dict:
        client = await self._get_client()
        response = await call_claude_with_resilience(
            client=client,
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        # ...
```

---

### Step 7: Add Health Check Endpoint

**File:** `backend/app/api/qa.py` - Add at end

```python
@router.get("/health")
async def qa_health_check() -> dict:
    """Check QA service health including circuit breaker state."""
    from app.services.qa.resilience import get_circuit_state

    circuit = get_circuit_state()

    return {
        "status": "healthy" if circuit["state"] != "open" else "degraded",
        "qa_enabled": settings.QA_ENABLED,
        "api_key_configured": bool(settings.ANTHROPIC_API_KEY),
        "circuit_breaker": circuit,
    }
```

---

## Testing

### Unit Test for Resilience

**File:** `backend/tests/test_services/test_resilience.py`

```python
import pytest
from unittest.mock import AsyncMock, patch
import anthropic

from app.services.qa.resilience import (
    call_claude_with_resilience,
    get_anthropic_client,
    claude_circuit_breaker,
)


class TestResilience:
    @pytest.fixture(autouse=True)
    def reset_circuit(self):
        """Reset circuit breaker before each test."""
        claude_circuit_breaker.close()
        yield

    async def test_retry_on_rate_limit(self):
        """Test retry on rate limit error."""
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=[
                anthropic.RateLimitError("Rate limited", response=None, body=None),
                anthropic.RateLimitError("Rate limited", response=None, body=None),
                AsyncMock(content=[AsyncMock(text="Success")]),
            ]
        )

        with patch("app.services.qa.resilience.settings") as mock_settings:
            mock_settings.ANTHROPIC_MAX_RETRIES = 3
            mock_settings.RETRY_BACKOFF_FACTOR = 0.1  # Fast for tests

            response = await call_claude_with_resilience(
                client=mock_client,
                model="test",
                max_tokens=100,
                messages=[{"role": "user", "content": "test"}],
            )

        assert mock_client.messages.create.call_count == 3

    async def test_circuit_opens_after_failures(self):
        """Test circuit breaker opens after threshold failures."""
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=anthropic.InternalServerError("Server error", response=None, body=None)
        )

        # Trigger failures up to threshold
        for _ in range(5):
            try:
                await call_claude_with_resilience(
                    client=mock_client,
                    model="test",
                    max_tokens=100,
                    messages=[{"role": "user", "content": "test"}],
                )
            except:
                pass

        assert claude_circuit_breaker.current_state == "open"
```

---

## Verification Commands

```bash
# 1. Add dependencies
cd backend && uv add tenacity aiobreaker

# 2. Run type check
uv run mypy app/services/qa/resilience.py

# 3. Run tests
uv run pytest tests/test_services/test_resilience.py -v

# 4. Test health endpoint
curl http://localhost:8000/api/v1/qa/health
```

---

## Expected Behavior After Implementation

| Scenario | Before | After |
|----------|--------|-------|
| Rate limit (429) | Silent failure | Retry 3x with backoff, then fail |
| API timeout | Hang indefinitely | Fail after 30s |
| API down | Every call fails | Circuit opens after 5 failures, recovers in 60s |
| Partial outage | Cascade failure | Isolated failure, graceful degradation |

---

## Summary

| Task | Files | Lines Changed |
|------|-------|---------------|
| Add deps | `pyproject.toml` | +2 |
| Add config | `config.py` | +5 |
| Create resilience module | `resilience.py` | ~100 (new) |
| Update evaluator | `evaluator.py` | ~15 |
| Update test_runner | `test_runner.py` | ~20 |
| Update test_caller | `test_caller.py` | ~25 |
| Add health endpoint | `qa.py` | +15 |
| Add tests | `test_resilience.py` | ~50 (new) |

**Total: ~230 lines of changes** - No overengineering, just the essentials.

---

## Sources

- [Tenacity - Python Retrying Library](https://github.com/jd/tenacity)
- [aiobreaker - Async Circuit Breaker](https://github.com/arlyon/aiobreaker)
- [LLM API Retry Logic 2025](https://markaicode.com/llm-api-retry-logic-implementation/)
- [Circuit Breaker Pattern FastAPI](https://dev.to/akarshan/building-resilient-database-operations-with-aiobreaker-async-sqlalchemy-fastapi-23dl)
- [Claude API 529 Error Handling](https://www.cursor-ide.com/blog/claude-code-api-error-529-overloaded)

---

*Plan Version: 1.0*
*Estimated Effort: 2-3 hours*
