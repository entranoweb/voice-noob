"""Circuit breaker pattern implementation for external service calls."""

import asyncio
import logging
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from app.core.log_redaction import describe_exception

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting calls
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open."""


class CircuitBreaker:
    """Circuit breaker to prevent cascading failures."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        timeout: float = 60.0,
        recovery_timeout: float = 30.0,
    ):
        """
        Initialize circuit breaker.

        Args:
            name: Name of the circuit (for logging)
            failure_threshold: Number of failures before opening circuit
            timeout: Time in seconds to wait before attempting recovery
            recovery_timeout: Time in seconds for recovery attempt
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.recovery_timeout = recovery_timeout

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self._lock = asyncio.Lock()

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Async function to execute
            *args: Positional arguments to pass to func
            **kwargs: Keyword arguments to pass to func

        Returns:
            Result from func

        Raises:
            CircuitBreakerError: If circuit is open
            Exception: Any exception raised by func
        """
        async with self._lock:
            # Check if we should attempt recovery
            if self.state == CircuitState.OPEN:
                if self._should_attempt_recovery():
                    logger.info("Circuit breaker %s entering HALF_OPEN state", self.name)
                    self.state = CircuitState.HALF_OPEN
                else:
                    raise CircuitBreakerError(
                        f"Circuit breaker {self.name} is OPEN - service unavailable"
                    )

        # Execute the function
        try:
            result = await func(*args, **kwargs)

            # Success - reset on successful call
            async with self._lock:
                if self.state == CircuitState.HALF_OPEN:
                    logger.info("Circuit breaker %s recovered - entering CLOSED state", self.name)
                    self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.last_failure_time = None

            return result

        except Exception as e:
            # Failure - increment counter and potentially open circuit
            async with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()

                logger.warning(
                    "Circuit breaker %s failure %d/%d: %s",
                    self.name,
                    self.failure_count,
                    self.failure_threshold,
                    describe_exception(e),
                )

                if self.failure_count >= self.failure_threshold:
                    logger.error(  # noqa: TRY400 - a traceback would carry the message
                        "Circuit breaker %s OPENED after %d failures: %s",
                        self.name,
                        self.failure_count,
                        describe_exception(e),
                    )
                    self.state = CircuitState.OPEN

            raise

    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        if self.last_failure_time is None:
            return True

        time_since_failure = time.time() - self.last_failure_time
        return time_since_failure >= self.timeout

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        logger.info("Circuit breaker %s manually reset", self.name)

    def get_state(self) -> dict[str, Any]:
        """Get current circuit breaker state."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "last_failure_time": self.last_failure_time,
        }
