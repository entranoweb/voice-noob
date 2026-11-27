"""Structured logging set-up."""

import structlog

from app.core.log_redaction import redact_event_dict


def configure_logging(level: int) -> None:
    """Send structlog events at or above ``level`` to stdout as JSON.

    ``redact_event_dict`` (the backstop that masks phone numbers and emails) is
    in the chain whatever the level.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_event_dict,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
