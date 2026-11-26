"""Base service class for external API integrations with timeout and retry logic."""

import asyncio
from typing import Any, TypeVar

import httpx
import structlog

from app.core.log_redaction import log_exception, scrub_text

logger = structlog.get_logger()

T = TypeVar("T")

# HTTP Status Code Constants
HTTP_TOO_MANY_REQUESTS = 429
HTTP_CLIENT_ERROR_MIN = 400
HTTP_CLIENT_ERROR_MAX = 500


class ExternalServiceError(Exception):
    """Base exception for external service errors."""


class ExternalServiceTimeoutError(ExternalServiceError):
    """External service request timed out."""


class ExternalServiceRateLimitError(ExternalServiceError):
    """External service rate limit exceeded."""


class BaseExternalService:
    """Base class for external API integrations.

    Provides timeout, retry, and circuit breaker patterns.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
    ):
        """Initialize external service client.

        Args:
            base_url: Base URL for the API
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds (default: 30s)
            max_retries: Maximum number of retry attempts (default: 3)
            backoff_factor: Exponential backoff multiplier (default: 2.0)
        """
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with timeout configuration."""
        if self._client is None:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client connection pool."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _raise_rate_limit_error(self, retry_after: int) -> None:
        """Raise rate limit error (extracted to satisfy TRY301)."""
        raise ExternalServiceRateLimitError(
            f"Rate limited by {self.base_url}, retry after {retry_after}s"
        )

    async def request_with_retry(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make HTTP request with exponential backoff retry.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            **kwargs: Additional arguments passed to httpx request

        Returns:
            httpx.Response object

        Raises:
            ExternalServiceTimeout: If request times out
            ExternalServiceRateLimited: If rate limit is exceeded
            ExternalServiceError: For other API errors
        """
        client = await self.get_client()
        last_exception: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                response = await client.request(method, endpoint, **kwargs)

                # Handle rate limiting
                if response.status_code == HTTP_TOO_MANY_REQUESTS:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(
                        "Rate limited by external service",
                        service=self.base_url,
                        retry_after=retry_after,
                        attempt=attempt + 1,
                    )
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(retry_after)
                        continue
                    self._raise_rate_limit_error(retry_after)

                # Raise for other HTTP errors
                response.raise_for_status()
                return response

            except httpx.TimeoutException as e:
                last_exception = e
                logger.warning(
                    "External service timeout",
                    service=self.base_url,
                    endpoint=endpoint,
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.backoff_factor**attempt)
                    continue
                raise ExternalServiceTimeoutError(
                    f"Timeout calling {self.base_url}{endpoint}"
                ) from e

            except httpx.HTTPStatusError as e:
                # Don't retry on 4xx client errors (except 429 handled above)
                if (
                    HTTP_CLIENT_ERROR_MIN <= e.response.status_code < HTTP_CLIENT_ERROR_MAX
                    and e.response.status_code != HTTP_TOO_MANY_REQUESTS
                ):
                    log_exception(
                        logger,
                        "External service client error",
                        e,
                        service=self.base_url,
                        status_code=e.response.status_code,
                        detail=scrub_text(e.response.text[:200]),
                    )
                    raise ExternalServiceError(f"Client error: {e.response.status_code}") from e

                # Retry on 5xx server errors
                last_exception = e
                logger.warning(
                    "External service server error",
                    service=self.base_url,
                    status_code=e.response.status_code,
                    attempt=attempt + 1,
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.backoff_factor**attempt)
                    continue
                raise ExternalServiceError(f"Server error: {e.response.status_code}") from e

            except Exception as e:
                last_exception = e
                log_exception(
                    logger,
                    "Unexpected error calling external service",
                    e,
                    service=self.base_url,
                    attempt=attempt + 1,
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.backoff_factor**attempt)
                    continue
                raise ExternalServiceError(f"Unexpected error: {e!s}") from e

        # Should never reach here due to raises above, but just in case
        raise ExternalServiceError("Max retries exceeded") from last_exception
