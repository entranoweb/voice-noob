"""Application configuration using Pydantic settings."""

from typing import Any

from pydantic import PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Application
    APP_NAME: str = "Voice Noob API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    RELOAD: bool = False
    PUBLIC_URL: str | None = None  # Public URL for webhook callbacks (e.g., ngrok URL)

    # Database
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "voicenoob"
    DATABASE_URL: PostgresDsn | None = None

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_connection(cls, v: str | None, info: Any) -> str:
        """Build database URL from components if not provided."""
        if isinstance(v, str):
            return v

        data = info.data
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=data.get("POSTGRES_USER"),
                password=data.get("POSTGRES_PASSWORD"),
                host=data.get("POSTGRES_SERVER"),
                port=data.get("POSTGRES_PORT"),
                path=f"{data.get('POSTGRES_DB') or ''}",
            ),
        )

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_URL: RedisDsn | None = None

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def assemble_redis_connection(cls, v: str | None, info: Any) -> str:
        """Build Redis URL from components if not provided."""
        if isinstance(v, str):
            return v

        data = info.data
        password_part = f":{data.get('REDIS_PASSWORD')}@" if data.get("REDIS_PASSWORD") else ""
        return f"redis://{password_part}{data.get('REDIS_HOST')}:{data.get('REDIS_PORT')}/{data.get('REDIS_DB')}"

    # Security
    SECRET_KEY: str = "change-this-to-a-random-secret-key-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CORS
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:8000",
    ]
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: list[str] = ["*"]
    CORS_ALLOW_HEADERS: list[str] = ["*"]

    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60

    # Default Admin User (created on first startup if no users exist)
    # IMPORTANT: Change these in production via environment variables!
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "CHANGE_ME_IN_PRODUCTION"
    ADMIN_NAME: str = "Admin"

    # Voice & AI Services
    OPENAI_API_KEY: str | None = None
    DEEPGRAM_API_KEY: str | None = None
    ELEVENLABS_API_KEY: str | None = None

    # Telephony
    TELNYX_API_KEY: str | None = None
    TELNYX_PUBLIC_KEY: str | None = None
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None

    # External Service Timeouts (seconds)
    # These are critical for preventing hung connections during voice calls
    OPENAI_TIMEOUT: float = 30.0  # LLM inference can be slow
    DEEPGRAM_TIMEOUT: float = 15.0  # Real-time STT should be fast
    ELEVENLABS_TIMEOUT: float = 20.0  # TTS synthesis timeout
    TELNYX_TIMEOUT: float = 10.0  # Telephony API calls
    TWILIO_TIMEOUT: float = 10.0  # Telephony API calls
    GOOGLE_API_TIMEOUT: float = 15.0  # Calendar, Drive, etc.
    DEFAULT_EXTERNAL_TIMEOUT: float = 30.0  # Fallback for other APIs

    # Retry Configuration
    MAX_RETRIES: int = 3  # Number of retry attempts for failed requests
    RETRY_BACKOFF_FACTOR: float = 2.0  # Exponential backoff multiplier

    # Monitoring
    SENTRY_DSN: str | None = None
    SENTRY_ENVIRONMENT: str = "development"
    SENTRY_TRACES_SAMPLE_RATE: float = 1.0

    # OpenTelemetry
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "voicenoob-api"
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

    # QA Testing Framework
    # Feature flags for post-call evaluation and testing
    QA_ENABLED: bool = False  # Master switch for QA features
    QA_AUTO_EVALUATE: bool = True  # Auto-evaluate calls on completion
    QA_EVALUATION_MODEL: str = "claude-sonnet-4-20250514"  # Claude model for evaluation
    QA_DEFAULT_THRESHOLD: int = 70  # Pass/fail score threshold (0-100)
    QA_MAX_CONCURRENT_EVALUATIONS: int = 5  # Max parallel evaluations
    QA_ALERT_ON_FAILURE: bool = True  # Send alerts on failed evaluations
    QA_ENABLE_LATENCY_TRACKING: bool = True  # Track response latency metrics
    QA_ENABLE_TURN_ANALYSIS: bool = True  # Analyze individual conversation turns
    QA_ENABLE_QUALITY_METRICS: bool = True  # Track coherence, relevance, etc.
    ANTHROPIC_API_KEY: str | None = None  # Claude API key for QA evaluation

    # Anthropic API Resilience Settings
    ANTHROPIC_TIMEOUT: float = 30.0  # Claude API request timeout (seconds)
    ANTHROPIC_MAX_RETRIES: int = 3  # Number of retry attempts
    ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD: int = 5  # Failures before circuit opens
    ANTHROPIC_CIRCUIT_RECOVERY_TIMEOUT: int = 60  # Seconds before circuit recovery

    # Production Hardening Feature Flags
    ENABLE_CALL_REGISTRY: bool = True  # Track active calls in Redis
    ENABLE_PROMETHEUS_METRICS: bool = True  # Expose /metrics endpoint
    ENABLE_CONNECTION_DRAINING: bool = True  # Graceful shutdown support
    ENABLE_CALL_QUEUE: bool = False  # Sprint 2: Call queuing

    # Production Hardening Settings
    SHUTDOWN_DRAIN_TIMEOUT: int = 120  # Seconds to wait for calls to drain
    CALL_REGISTRY_TTL: int = 1800  # 30 minutes TTL for call entries
    MAX_CALL_QUEUE_SIZE: int = 1000  # Max queued calls (Sprint 2)


settings = Settings()
