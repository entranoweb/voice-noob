"""Audit trail for security-relevant actions.

``record_audit`` does two things for each action:

* adds an :class:`~app.models.audit_event.AuditEvent` row to the caller's
  session, so the record commits or rolls back with the change it describes;
* writes one structured log line through a logger that is not subject to the
  application's log level, so production (which logs warnings and above) still
  keeps it.

Details are sanitised before either: a credential never reaches the trail.
"""

import logging
import sys
import uuid
from typing import Any

import structlog
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent


class _CurrentStdout:
    """Writes to whatever ``sys.stdout`` is now, not what it was at import."""

    def write(self, text: str) -> int:
        return sys.stdout.write(text)

    def flush(self) -> None:
        sys.stdout.flush()


# Built apart from the application's logger configuration so the INFO floor here
# holds whatever level the rest of the application logs at.
_audit_logger = structlog.wrap_logger(
    structlog.PrintLogger(file=_CurrentStdout()),  # type: ignore[arg-type]
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    cache_logger_on_first_use=False,
).bind(logger="audit", audit=True)

_MASK_SUFFIX_LENGTH = 4
_MASK_MIN_LENGTH = 12
_MAX_DETAIL_DEPTH = 4

_SENSITIVE_MARKERS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "private_key",
)


class AuditAction:
    """Audit action names."""

    # Authentication
    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILED = "auth.login.failed"
    REGISTER = "auth.register"

    # Provider credentials
    CREDENTIAL_UPDATE = "credential.update"

    # Integrations
    INTEGRATION_CONNECT = "integration.connect"
    INTEGRATION_UPDATE = "integration.update"
    INTEGRATION_DISCONNECT = "integration.disconnect"

    # Agents
    AGENT_CREATE = "agent.create"
    AGENT_UPDATE = "agent.update"
    AGENT_DELETE = "agent.delete"

    # Phone numbers
    PHONE_NUMBER_ASSIGN = "phone_number.assign"

    # Data protection
    DATA_EXPORT = "compliance.data_export"
    DATA_ERASURE = "compliance.data_erasure"
    RETENTION_SWEEP = "compliance.retention_sweep"
    CONSENT_UPDATE = "compliance.consent_update"

    # Results uploaded from the caller rig
    CALLER_RUN_UPLOAD = "caller_run.upload"
    CALLER_RUN_DELETE = "caller_run.delete"

    # Administration
    ADMIN_BOOTSTRAP = "admin.bootstrap"


def sanitize_details(details: dict[str, Any], _depth: int = 0) -> dict[str, Any]:
    """Mask anything that looks like a credential, at any depth."""
    clean: dict[str, Any] = {}
    for key, value in details.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in _SENSITIVE_MARKERS):
            clean[key] = _mask(value)
        elif isinstance(value, dict):
            clean[key] = (
                sanitize_details(value, _depth + 1) if _depth < _MAX_DETAIL_DEPTH else "..."
            )
        else:
            clean[key] = value
    return clean


def _mask(value: Any) -> str:
    if isinstance(value, str) and len(value) >= _MASK_MIN_LENGTH:
        return f"****{value[-_MASK_SUFFIX_LENGTH:]}"
    return "****"


def client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


async def record_audit(
    db: AsyncSession,
    action: str,
    *,
    actor_user_id: int | None = None,
    workspace_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
    request: Request | None = None,
    commit: bool = False,
) -> AuditEvent:
    """Record one audited action.

    The row joins the caller's transaction. Pass ``commit=True`` only where
    nothing else will commit, such as a refused login.
    """
    clean = sanitize_details(details) if details else None
    ip_address = client_ip(request)
    audit_event = AuditEvent(
        actor_user_id=actor_user_id,
        workspace_id=workspace_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        success=success,
        ip_address=ip_address,
        details=clean,
    )
    db.add(audit_event)
    if commit:
        await db.commit()

    log = _audit_logger.info if success else _audit_logger.warning
    log(
        "audit_event",
        action=action,
        actor_user_id=actor_user_id,
        workspace_id=str(workspace_id) if workspace_id else None,
        target_type=target_type,
        target_id=target_id,
        success=success,
        ip_address=ip_address,
        details=clean,
    )
    return audit_event
