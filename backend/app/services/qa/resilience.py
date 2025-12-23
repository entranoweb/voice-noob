"""Resilience patterns for QA services.

Provides retry, timeout, and circuit breaker for Claude API calls.
Based on 2025 production patterns from anthropic-sdk-python, fastapi-langgraph-agent,
and livekit/agents repositories.
"""

from __future__ import annotations

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
# Opens after ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD failures
# Recovers after ANTHROPIC_CIRCUIT_RECOVERY_TIMEOUT seconds
claude_circuit_breaker = CircuitBreaker(
    fail_max=settings.ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD,
    timeout_duration=settings.ANTHROPIC_CIRCUIT_RECOVERY_TIMEOUT,
    name="claude_api",
)


def get_anthropic_client() -> anthropic.AsyncAnthropic:
    """Get Anthropic client with timeout configured.

    Returns:
        AsyncAnthropic client with proper timeout settings.

    Raises:
        ValueError: If ANTHROPIC_API_KEY not configured.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    return anthropic.AsyncAnthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=httpx.Timeout(settings.ANTHROPIC_TIMEOUT, connect=5.0),
    )


def _create_retry_decorator() -> retry:  # type: ignore[valid-type]
    """Create retry decorator with current settings.

    Uses tenacity with exponential backoff for transient errors.
    Retries on: RateLimitError (429), APIConnectionError, InternalServerError (5xx),
    and httpx.TimeoutException (network timeouts).
    """
    return retry(
        stop=stop_after_attempt(settings.ANTHROPIC_MAX_RETRIES),
        wait=wait_exponential(
            multiplier=settings.RETRY_BACKOFF_FACTOR,
            max=30,
        ),
        retry=retry_if_exception_type(
            (
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.InternalServerError,
                httpx.TimeoutException,
            )
        ),
        before_sleep=lambda retry_state: logger.warning(
            "claude_api_retry",
            attempt=retry_state.attempt_number,
            wait=getattr(retry_state.next_action, "sleep", None),
        ),
    )


# Apply retry decorator
_retry_decorator = _create_retry_decorator()


@_retry_decorator  # type: ignore[misc, untyped-decorator]
async def call_claude_with_resilience(
    client: anthropic.AsyncAnthropic,
    model: str,
    max_tokens: int,
    messages: list[dict[str, str]],
    system: str | None = None,
) -> anthropic.types.Message:
    """Call Claude API with retry and circuit breaker.

    Implements the three key resilience patterns:
    1. Timeout: Via httpx.Timeout in client initialization
    2. Retry: Via tenacity with exponential backoff for transient errors
    3. Circuit breaker: Via aiobreaker to prevent cascade failures

    Args:
        client: Anthropic async client (created with get_anthropic_client).
        model: Model name (e.g., "claude-sonnet-4-20250514").
        max_tokens: Maximum tokens to generate.
        messages: List of message dicts with "role" and "content".
        system: Optional system prompt.

    Returns:
        Claude API Message response.

    Raises:
        CircuitBreakerError: If circuit is open (too many recent failures).
        anthropic.RateLimitError: If rate limit exceeded after all retries.
        anthropic.APIConnectionError: If connection failed after all retries.
        anthropic.InternalServerError: If server error after all retries.
        anthropic.APIError: For other API errors (not retried).
    """
    # Check circuit breaker state before attempting call
    if claude_circuit_breaker.current_state == "open":
        logger.error(
            "claude_circuit_open",
            recovery_in=claude_circuit_breaker.timeout_duration,
        )
        raise CircuitBreakerError(claude_circuit_breaker)

    try:
        async with claude_circuit_breaker:
            if system:
                return await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=messages,  # type: ignore[arg-type]
                    system=system,
                )
            return await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,  # type: ignore[arg-type]
            )

    except anthropic.APIError as e:
        logger.warning(
            "claude_api_error",
            error=str(e),
            status=getattr(e, "status_code", None),
        )
        raise


def is_circuit_open() -> bool:
    """Check if circuit breaker is open.

    Returns:
        True if circuit is open (Claude API unavailable).
    """
    state_str = str(claude_circuit_breaker.current_state)
    return "open" in state_str.lower()


def get_circuit_state() -> dict[str, str | int]:
    """Get circuit breaker state for monitoring.

    Returns:
        Dict with state, fail_count, and fail_max.
    """
    # Convert CircuitBreakerState enum to string
    state_str = str(claude_circuit_breaker.current_state)
    # Extract just the state name (e.g., "CLOSED" from "CircuitBreakerState.CLOSED")
    if "." in state_str:
        state_str = state_str.split(".")[-1].lower()

    return {
        "state": state_str,
        "fail_count": claude_circuit_breaker.fail_counter,
        "fail_max": claude_circuit_breaker.fail_max,
    }


def reset_circuit_breaker() -> None:
    """Reset circuit breaker to closed state.

    Useful for testing or manual recovery.
    """
    claude_circuit_breaker.close()
    logger.info("claude_circuit_reset")


__all__ = [
    "CircuitBreakerError",
    "call_claude_with_resilience",
    "get_anthropic_client",
    "get_circuit_state",
    "is_circuit_open",
    "reset_circuit_breaker",
]
