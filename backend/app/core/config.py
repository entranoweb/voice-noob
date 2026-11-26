"""Application configuration using Pydantic settings."""

import os
from typing import Any

from pydantic import PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value SECRET_KEY shipped with. It is public, so anything signed with it is
# forgeable by anyone who has read this file.
INSECURE_DEFAULT_SECRET_KEY = "change-this-to-a-random-secret-key-in-production"

# Placeholders the example files and the compose default have carried. Each is as
# public as the default, and a deployment that copied one unchanged is as open.
PLACEHOLDER_SECRET_KEYS = frozenset(
    {
        INSECURE_DEFAULT_SECRET_KEY,
        "generate_a_secure_key_here",
        "your_64_character_secret_key_here",
        "your_64_character_hex_string_here",
    }
)

# Passwords the repository has shipped for the database and for Redis, in the
# compose file and the example environment files. Each is public. A production
# start on any of them is refused, the same way a placeholder SECRET_KEY is.
# ("postgres", the development default, is not listed: the production compose
# file no longer has a default at all, and CI runs on it.)
PLACEHOLDER_SERVICE_PASSWORDS = frozenset(
    {
        "changeme",
        "changeme_in_production",
        "change_this_password",
        "your_secure_postgres_password_here",
        "your_secure_redis_password_here",
    }
)

# Passwords the repository has shipped for the first administrator. Kept in
# their original spelling because start-up tests stored hashes against them.
SHIPPED_ADMIN_PASSWORDS = (
    "changeme",
    "CHANGE_ME_IN_PRODUCTION",
    "CHANGE_ME_BEFORE_RUNNING",
    "your_secure_admin_password",
)


# How long gunicorn lets a worker finish on shutdown before it is killed
# (gunicorn.conf.py reads this, and docker-compose.prod.yml's stop_grace_period
# must exceed it). Shutdown spends it on, in order: each live call's tool grace
# (CALL_TOOL_GRACE_S), the database writes that save the call, and the registry
# drain (SHUTDOWN_DRAIN_TIMEOUT). Settings refuses a combination that does not
# fit, because a worker killed mid-save loses the call record silently.
GUNICORN_GRACEFUL_TIMEOUT_S = 40
# What the saves between the tool grace and the drain are allowed to take.
SHUTDOWN_SAVE_MARGIN_S = 5


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # SETTINGS_ENV_FILE names the env file to read; empty reads none. The test
    # suite sets it empty, so a developer's own backend/.env (a local
    # PUBLIC_URL, say) cannot change what a test sees. Real environment
    # variables are read either way and always win over the file.
    model_config = SettingsConfigDict(
        env_file=os.environ.get("SETTINGS_ENV_FILE", ".env") or None,
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Application
    APP_NAME: str = "Synthiq Voice API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    RELOAD: bool = False
    PUBLIC_URL: str | None = None  # Public URL for webhook callbacks (e.g., ngrok URL)
    # Origin of the web app, which also serves the embeddable widget. Optional:
    # the embed snippet falls back to the first CORS origin, then to PUBLIC_URL.
    FRONTEND_URL: str | None = None

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
    SECRET_KEY: str = INSECURE_DEFAULT_SECRET_KEY
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # Accept carrier webhooks that carry no verifiable signature. Off by default
    # and meant to stay off: it exists so a local rehearsal without a carrier key
    # is a deliberate, named choice instead of a side effect of DEBUG. With it on,
    # anyone who can reach the webhook can post a call to this instance.
    ALLOW_UNSIGNED_WEBHOOKS: bool = False
    # Keys that encrypt customer credentials at rest (app.core.secret_box):
    # "<id>:<base64 32-byte key>", comma separated. The first entry encrypts;
    # every entry can decrypt, which is how a key is rotated: put the new key
    # first, keep the old one behind it, run scripts/rotate_credential_keys.py,
    # then drop the old one. Removing a key while rows still name it makes those
    # credentials unreadable. Required outside DEBUG.
    CREDENTIAL_ENCRYPTION_KEYS: str | None = None
    # How old a signed Telnyx webhook may be, in seconds. The timestamp is part
    # of what Telnyx signs; without an age limit a captured webhook can be
    # replayed for ever, and the reply to an inbound one carries a fresh token
    # that admits the sender to the agent. Telnyx recommends five minutes.
    TELNYX_WEBHOOK_MAX_AGE_SECONDS: int = 300
    # Host names this server may call even though they resolve to a private
    # address (app.core.safe_http). Empty by default. For an operator's own
    # private endpoint only; never a value a customer supplies.
    OUTBOUND_ALLOW_PRIVATE_HOSTS: list[str] = []
    # Whether anyone may create an account at /api/v1/auth/register. Unset, it
    # follows DEBUG: open on a development machine, closed in production, where
    # the administrator creates accounts (POST /api/v1/auth/users).
    ALLOW_PUBLIC_SIGNUP: bool | None = None
    # Serve /docs, /redoc and /openapi.json. Unset, it follows DEBUG.
    ENABLE_API_DOCS: bool | None = None
    # Bearer token a scraper must present to read /metrics outside DEBUG. The
    # metrics carry agent ids as labels. Unset in production, /metrics answers
    # 404 rather than serving them to anyone who asks.
    METRICS_TOKEN: str | None = None

    # Public embed widget (app.core.embed_session).
    # An agent with no allowed domains can be embedded by no other site. This
    # restores the old meaning, "every site", for a deployment that needs time to
    # fill its allow-lists in. It logs a warning each time it is why a request
    # was served. Leave it off.
    EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET: bool = False
    # The longest an embedded session can run, and the lifetime of its token.
    EMBED_SESSION_MAX_SECONDS: int = 900
    # Whether embedded sessions may call an agent's tools when the agent's own
    # embed settings do not say (embed_settings.allow_tools). Off: a tool call
    # from the widget acts with the owner's integrations on an anonymous
    # visitor's input.
    EMBED_TOOLS_DEFAULT_ENABLED: bool = False

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
    # Where rate-limit counts are kept (app.core.limiter). Unset: Redis, at
    # REDIS_URL, so every worker shares one count. "memory://" counts per
    # process, which is only right for a single-process deployment.
    RATE_LIMIT_STORAGE_URI: str | None = None

    # Default Admin User (created on first startup if no users exist)
    # IMPORTANT: Change these in production via environment variables!
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "CHANGE_ME_IN_PRODUCTION"
    ADMIN_NAME: str = "Admin"

    # Voice & AI Services
    OPENAI_API_KEY: str | None = None

    # The one place Realtime model names live. Nothing else in the backend (or
    # the frontend tier cards, which mirror these) spells a model name out.
    # Agents choose a tier, not a model: the tier maps to one of these two at
    # session start, so an agent or workspace row that still stores a retired
    # name in `llm_model` or an Azure deployment keeps working with no data
    # migration. Override per deployment with the environment variables of the
    # same name.
    # UNVERIFIED until a live call: that OpenAI serves these two names as
    # configured. See the synthiq-voice-calls skill.
    OPENAI_REALTIME_MODEL_PREMIUM: str = "gpt-realtime-2.1"
    OPENAI_REALTIME_MODEL_PREMIUM_MINI: str = "gpt-realtime-2.1-mini"
    # Transcription of the caller's speech inside a Realtime session. Empty
    # means "send no transcription block": the call still works, there are just
    # no caller transcripts. `gpt-transcribe` is the documented choice for a
    # Realtime session: the realtime-transcription guide
    # (https://developers.openai.com/api/docs/guides/realtime-transcription)
    # describes it performing "input transcription in a Realtime API session" or
    # in a dedicated transcription session. `gpt-live-transcribe` is not usable
    # here: the same guide says it "doesn't support `server_vad` or
    # `semantic_vad`", and every session this app opens sets server_vad.
    # UNVERIFIED until a live call: the docs' no-VAD rule names two other
    # transcription models (see the synthiq-voice-calls skill), so `gpt-transcribe`
    # with `server_vad` is not excluded, but no call has shown it works.
    OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL: str = "gpt-transcribe"
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
    OTEL_SERVICE_NAME: str = "synthiq-voice-api"
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    # Allow spans to leave the host over plain HTTP. Off by default: a call trace
    # carries transcripts and the tool arguments built from them, so an
    # unencrypted hop to another machine puts caller speech on the wire. A
    # loopback collector needs no opt-in; an in-cluster one over HTTP does, and
    # setting this is the operator saying that network is trusted.
    OTEL_ALLOW_INSECURE_EXPORT: bool = False

    # QA Testing Framework
    # Feature flags for post-call evaluation and testing
    QA_ENABLED: bool = False  # Master switch for QA features
    QA_AUTO_EVALUATE: bool = True  # Auto-evaluate calls on completion
    QA_EVALUATION_MODEL: str = "claude-sonnet-4-6"  # Post-call evaluation of real calls

    # A simulated run uses three models in three different roles, and they do
    # not need the same capability. Pointing all three at one frontier model was
    # costing roughly three times what the work requires.
    #
    #   agent  — stands in for the agent under test. This one is the measurement,
    #            so a weaker model here makes the agent look worse than it is.
    #            Do not economise on it.
    #   caller — plays a person on the phone with a goal. Needs to follow a
    #            persona and answer questions, nothing more.
    #   judge  — scores the qualitative dimensions deterministic metrics cannot.
    #            Off by default in CI, and cheap when on.
    #
    # Before moving the caller or judge to a smaller model on a real workload,
    # measure it: run both against the same labelled set and compare agreement.
    # A judge that disagrees with a human is not cheaper, it is wrong.
    QA_AGENT_MODEL: str = "claude-sonnet-4-6"
    QA_CALLER_MODEL: str = "claude-haiku-4-5"
    QA_JUDGE_MODEL: str = "claude-haiku-4-5"

    # Base URL for an OpenAI-compatible endpoint serving open-weight models —
    # vLLM, Ollama, Groq, Together, OpenRouter, or anything else speaking that
    # API. Set it and the caller and judge run there instead of Anthropic.
    # This is not only a cost lever: a harness that must run inside a bank's
    # own network cannot depend on an external API for any part of a run.
    QA_OPEN_MODEL_BASE_URL: str | None = None
    QA_OPEN_MODEL_API_KEY: str | None = None
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
    # Seconds to wait for calls to drain. On SIGTERM uvicorn closes every call's
    # socket at once, so this only waits on registry entries that outlive their
    # call. It runs inside the worker's gunicorn graceful_timeout (40 s) after
    # the bridges' tool grace and database writes, so it must stay well below:
    # a longer drain is cut off by the SIGKILL that follows.
    SHUTDOWN_DRAIN_TIMEOUT: int = 15
    CALL_REGISTRY_TTL: int = 1800  # 30 minutes TTL for call entries
    MAX_CALL_QUEUE_SIZE: int = 1000  # Max queued calls (Sprint 2)

    # How long one phone call may run before the bridge ends it (MAX_DURATION).
    # The model provider's own ceiling is 60 minutes; the default leaves room
    # for a long booking call without letting a stuck one hold a model session.
    CALL_MAX_DURATION_S: float = 1800.0
    # Seconds with no conversation (nobody speaking, no agent audio, no tool
    # running) before the bridge ends the call (SILENCE_TIMEOUT). 0 turns it off.
    CALL_IDLE_TIMEOUT_S: float = 120.0
    # When the call ends while a tool is running (a booking write, say), how long
    # the tool may keep running before it is cancelled.
    CALL_TOOL_GRACE_S: float = 10.0

    # Data retention sweep (app.services.data_protection). Deletes call data
    # older than each account's retention period. Off unless an operator turns
    # it on: it deletes, and an upgrade must never start deleting on its own.
    # The first sweep after it is turned on only counts what it would delete.
    # One process at a time holds the lock, so several workers do not sweep at once.
    RETENTION_SWEEP_ENABLED: bool = False
    RETENTION_SWEEP_INTERVAL_HOURS: int = 24

    # Audio of caller calls (app.services.audio_store). A directory on local disk:
    # in a container it must be a persistent volume, or every recording is lost
    # on the next deploy. Stored per workspace under generated names.
    CALLER_AUDIO_DIR: str = "./data/caller-audio"
    # The most one uploaded file may be. 64 MiB is about 33 minutes of 16 kHz mono.
    CALLER_AUDIO_MAX_BYTES: int = 64 * 1024 * 1024
    # How often expired audio is deleted. Always on (it is not tied to
    # RETENTION_SWEEP_ENABLED): a pass also runs at start-up. Minimum 1.
    CALLER_AUDIO_EXPIRY_INTERVAL_MINUTES: int = 60

    @model_validator(mode="after")
    def shutdown_fits_in_graceful_timeout(self) -> "Settings":
        """Tool grace + saves + drain must finish before gunicorn kills the worker."""
        needed = self.CALL_TOOL_GRACE_S + SHUTDOWN_SAVE_MARGIN_S + self.SHUTDOWN_DRAIN_TIMEOUT
        if needed > GUNICORN_GRACEFUL_TIMEOUT_S:
            raise ValueError(
                f"CALL_TOOL_GRACE_S ({self.CALL_TOOL_GRACE_S:g}) + SHUTDOWN_DRAIN_TIMEOUT "
                f"({self.SHUTDOWN_DRAIN_TIMEOUT}) + {SHUTDOWN_SAVE_MARGIN_S} s for saving "
                f"is {needed:g} s, more than gunicorn's graceful timeout of "
                f"{GUNICORN_GRACEFUL_TIMEOUT_S} s: the worker would be killed mid-save. "
                "Lower one of them."
            )
        return self

    @property
    def public_signup_allowed(self) -> bool:
        """Whether /auth/register accepts a stranger."""
        if self.ALLOW_PUBLIC_SIGNUP is None:
            return self.DEBUG
        return self.ALLOW_PUBLIC_SIGNUP

    @property
    def api_docs_enabled(self) -> bool:
        """Whether the interactive API documentation is served."""
        if self.ENABLE_API_DOCS is None:
            return self.DEBUG
        return self.ENABLE_API_DOCS


class InsecureSecretKeyError(RuntimeError):
    """The application was started on a signing key anyone can read."""


def refuse_placeholder_secret_key(candidate: Settings) -> None:
    """Refuse to start on a shipped signing key outside DEBUG.

    The key signs login tokens and the tokens that admit a carrier's media stream
    to an agent. The shipped value and the example placeholders are in the
    repository, so running on one means both can be forged by anyone. Failing
    here, at import, is loud and immediate; the alternative is a deployment that
    works and is open.

    This is a plain check after the settings are built, not a pydantic
    validator, on purpose: a validation error prints the input it rejected, and
    the input is the process environment. A startup failure about one secret
    must not write fragments of the others into the log.
    """
    if candidate.DEBUG:
        return
    key = candidate.SECRET_KEY.strip()
    if not key or key in PLACEHOLDER_SECRET_KEYS:
        message = (
            "SECRET_KEY is unset or still a placeholder shipped in the repository. "
            "Set SECRET_KEY to a long random string (for example "
            "`openssl rand -hex 32`). It signs login tokens and call stream "
            "tokens, so a known value would let anyone forge both. For a throwaway "
            "local run only, DEBUG=true skips this check."
        )
        raise InsecureSecretKeyError(message)


class MissingCredentialKeyError(RuntimeError):
    """The application was started with no key to encrypt customer credentials."""


def refuse_missing_credential_keys(candidate: Settings) -> None:
    """Refuse to start outside DEBUG without a usable credential key ring.

    Without a key, customers' model, carrier and CRM keys would be written to
    the database in plain text while the dashboard says they are protected. A
    malformed ring is refused in every mode: a typo here must not silently turn
    encryption off.

    A plain check for the same reason as the signing-key check above: a pydantic
    validation error prints the value it rejected.
    """
    from app.core.secret_box import CredentialKeyError, parse_key_ring

    try:
        keys = parse_key_ring(candidate.CREDENTIAL_ENCRYPTION_KEYS)
    except CredentialKeyError as exc:
        raise MissingCredentialKeyError(str(exc)) from None
    if keys or candidate.DEBUG:
        return
    message = (
        "CREDENTIAL_ENCRYPTION_KEYS is not set. It encrypts the provider and "
        "integration credentials customers store, so the application will not "
        "start without it. Set it to '<id>:<key>', for example "
        "`python -c \"import secrets,base64;print('k1:' + "
        'base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`, and keep '
        "a copy somewhere safe: losing it loses every stored credential. For a "
        "throwaway local run only, DEBUG=true skips this check."
    )
    raise MissingCredentialKeyError(message)


class InsecureDefaultCredentialsError(RuntimeError):
    """The application was started on a credential shipped in the repository."""


def _password_in_url(url: str | None) -> str | None:
    """The password part of a connection URL, if it has one."""
    if not url:
        return None
    from urllib.parse import unquote, urlsplit

    try:
        password = urlsplit(str(url)).password
    except ValueError:
        return None
    return unquote(password) if password else None


def is_placeholder_service_password(value: str | None) -> bool:
    """Whether a database or Redis password is one the repository has shipped."""
    if value is None:
        return False
    return value.strip().lower() in PLACEHOLDER_SERVICE_PASSWORDS


def is_shipped_admin_password(value: str | None) -> bool:
    """Whether an administrator password is one the repository has shipped."""
    if value is None:
        return False
    return value.strip().lower() in {p.lower() for p in SHIPPED_ADMIN_PASSWORDS}


def refuse_default_credentials(candidate: Settings) -> None:
    """Refuse to start outside DEBUG on a shipped database or Redis password.

    The production compose file used to fall back to a fixed password for both
    when none was set, so a deployment that skipped the step ran on a value
    anyone can read in the repository.

    The administrator's password is checked separately, at start-up rather
    than at import (`app.core.first_admin`): it only matters to a process that
    is about to serve requests, and that check also needs the database.

    The message names the settings and never their values, for the reason given
    on `refuse_placeholder_secret_key`.
    """
    if candidate.DEBUG:
        return
    offending: list[str] = []
    if is_placeholder_service_password(_password_in_url(str(candidate.DATABASE_URL))):
        offending.append("the database password (POSTGRES_PASSWORD / DATABASE_URL)")
    if is_placeholder_service_password(
        _password_in_url(str(candidate.REDIS_URL))
    ) or is_placeholder_service_password(candidate.REDIS_PASSWORD):
        offending.append("the Redis password (REDIS_PASSWORD / REDIS_URL)")
    if offending:
        message = (
            "Refusing to start on a default credential shipped in the repository: "
            + "; ".join(offending)
            + ". Set each to a value of your own. For a throwaway local run only, "
            "DEBUG=true skips this check."
        )
        raise InsecureDefaultCredentialsError(message)


settings = Settings()
refuse_placeholder_secret_key(settings)
refuse_missing_credential_keys(settings)
refuse_default_credentials(settings)
