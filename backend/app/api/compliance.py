"""Compliance API routes for GDPR and CCPA."""

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditAction, record_audit
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.config import settings as app_settings
from app.core.secret_box import get_box
from app.db.session import get_db
from app.models.agent import Agent
from app.models.appointment import Appointment
from app.models.call_interaction import CallInteraction
from app.models.call_record import CallRecord
from app.models.caller_audio import CallerAudio
from app.models.campaign import Campaign
from app.models.compliance_run import (
    KIND_ERASURE,
    KIND_EXPORT,
    KIND_RETENTION,
    STATUS_SUCCEEDED,
    TRIGGER_SCHEDULE,
    TRIGGER_USER,
    ComplianceRun,
)
from app.models.contact import Contact
from app.models.phone_number import PhoneNumber
from app.models.privacy_settings import ConsentRecord, PrivacySettings
from app.models.user_integration import UserIntegration
from app.models.user_settings import UserSettings
from app.models.workspace import Workspace
from app.services.data_protection import (
    DRY_RUN,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    RECORDINGS_LEFT,
    call_record_owner_filter,
    contact_owner_filter,
    erase_account_data,
    finish_run,
    latest_run,
    owned_agent_ids,
    owned_workspace_ids,
    record_failed_run,
    run_retention,
    start_run,
)

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])
logger = structlog.get_logger()

# Constants
MAX_COMPLIANT_RETENTION_DAYS = 365


# =============================================================================
# Pydantic Models
# =============================================================================


class ComplianceCheckItem(BaseModel):
    """Single compliance check item."""

    id: str
    label: str
    description: str
    status: str  # "complete", "incomplete", "warning"
    action_url: str | None = None
    action_label: str | None = None


class GDPRStatusResponse(BaseModel):
    """GDPR compliance status response."""

    completed: int
    total: int
    percentage: int
    checks: list[ComplianceCheckItem]


class CCPAStatusResponse(BaseModel):
    """CCPA compliance status response."""

    completed: int
    total: int
    percentage: int
    checks: list[ComplianceCheckItem]


class ComplianceOverviewResponse(BaseModel):
    """Combined compliance overview."""

    gdpr: GDPRStatusResponse
    ccpa: CCPAStatusResponse


class PrivacySettingsResponse(BaseModel):
    """Privacy settings response."""

    privacy_policy_url: str | None
    privacy_policy_accepted_at: datetime | None
    data_retention_days: int
    openai_dpa_signed: bool
    openai_dpa_signed_at: datetime | None
    telnyx_dpa_signed: bool
    telnyx_dpa_signed_at: datetime | None
    deepgram_dpa_signed: bool
    deepgram_dpa_signed_at: datetime | None
    elevenlabs_dpa_signed: bool
    elevenlabs_dpa_signed_at: datetime | None
    ccpa_opt_out: bool
    ccpa_opt_out_at: datetime | None
    last_data_export_at: datetime | None


class UpdatePrivacySettingsRequest(BaseModel):
    """Request to update privacy settings."""

    privacy_policy_url: str | None = None
    # Bounded: 0 or a negative number would make every call the account has
    # "older than the period".
    data_retention_days: int | None = Field(
        default=None, ge=MIN_RETENTION_DAYS, le=MAX_RETENTION_DAYS
    )
    openai_dpa_signed: bool | None = None
    telnyx_dpa_signed: bool | None = None
    deepgram_dpa_signed: bool | None = None
    elevenlabs_dpa_signed: bool | None = None
    ccpa_opt_out: bool | None = None


class ConsentRequest(BaseModel):
    """Request to record consent."""

    consent_type: str
    granted: bool


class DataExportResponse(BaseModel):
    """Data export response containing all user data."""

    user: dict[str, object]
    settings: dict[str, object] | None
    privacy_settings: dict[str, object] | None
    agents: list[dict[str, object]]
    workspaces: list[dict[str, object]]
    contacts: list[dict[str, object]]
    appointments: list[dict[str, object]]
    call_records: list[dict[str, object]]
    call_interactions: list[dict[str, object]]
    consent_records: list[dict[str, object]]
    phone_numbers: list[dict[str, object]] = []
    campaigns: list[dict[str, object]] = []
    integrations: list[dict[str, object]] = []
    # Recordings of caller calls held in the audio store: what and until when, not the sound.
    caller_audio: list[dict[str, object]] = []
    exported_at: datetime


class DataDeletionResponse(BaseModel):
    """Response after data deletion."""

    deleted_counts: dict[str, int]
    deleted_at: datetime
    # False while something of the account's is still held where this erasure
    # cannot reach it.
    complete: bool = True
    # Recordings a carrier still holds, by the carrier's own call id. Delete
    # them in the carrier's account.
    not_erased_here: list[dict[str, str]] = []


# =============================================================================
# Helper Functions
# =============================================================================


async def get_or_create_privacy_settings(user_id: int, db: AsyncSession) -> PrivacySettings:
    """Get or create privacy settings for a user."""
    result = await db.execute(select(PrivacySettings).where(PrivacySettings.user_id == user_id))
    settings = result.scalar_one_or_none()

    if not settings:
        settings = PrivacySettings(user_id=user_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

    return settings


async def get_user_settings(user_id: int, db: AsyncSession) -> UserSettings | None:
    """Get user-level settings (API keys etc).

    Note: This returns user-level settings (where workspace_id is NULL),
    not workspace-specific settings. For compliance purposes, we check
    user-level API key configuration.
    """
    user_uuid = user_id_to_uuid(user_id)
    result = await db.execute(
        select(UserSettings).where(
            UserSettings.user_id == user_uuid,
            UserSettings.workspace_id.is_(None),
        )
    )
    return result.scalar_one_or_none()


def _run_check(
    check_id: str,
    label: str,
    run: ComplianceRun | None,
    *,
    never: str,
    succeeded: str,
    failed: str,
) -> ComplianceCheckItem:
    """A check whose status is what the last recorded run actually did."""
    if run is None:
        return ComplianceCheckItem(id=check_id, label=label, description=never, status="warning")
    when = run.started_at.date().isoformat()
    left = int((run.counts or {}).get(RECORDINGS_LEFT, 0)) if run.status == STATUS_SUCCEEDED else 0
    if left:
        return ComplianceCheckItem(
            id=check_id,
            label=label,
            description=(
                f"{succeeded} {when}, but {left} call recording(s) are still held by the "
                "carrier and were not erased here"
            ),
            status="warning",
        )
    if run.status == STATUS_SUCCEEDED:
        return ComplianceCheckItem(
            id=check_id, label=label, description=f"{succeeded} {when}", status="complete"
        )
    return ComplianceCheckItem(
        id=check_id, label=label, description=f"{failed} {when}", status="incomplete"
    )


def _retention_check(retention_days: int, last_sweep: ComplianceRun | None) -> ComplianceCheckItem:
    """Retention is in force only if the scheduled sweep has run recently."""
    label = "Data Retention Policy"
    period = f"Data retained for {retention_days} days."
    if not app_settings.RETENTION_SWEEP_ENABLED:
        return ComplianceCheckItem(
            id="data_retention",
            label=label,
            description=f"{period} The retention sweep is switched off, so nothing is deleted.",
            status="incomplete",
        )
    if last_sweep is None:
        return ComplianceCheckItem(
            id="data_retention",
            label=label,
            description=f"{period} The retention sweep has not run yet.",
            status="incomplete",
        )
    when = last_sweep.started_at.date().isoformat()
    if last_sweep.status != STATUS_SUCCEEDED:
        return ComplianceCheckItem(
            id="data_retention",
            label=label,
            description=f"{period} The last retention sweep failed on {when}.",
            status="incomplete",
        )
    if (last_sweep.counts or {}).get(DRY_RUN):
        return ComplianceCheckItem(
            id="data_retention",
            label=label,
            description=(
                f"{period} The first sweep ran on {when} as a rehearsal: it counted what it "
                "would delete and deleted nothing. Deletion starts with the next sweep."
            ),
            status="warning",
        )
    started = last_sweep.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    overdue = datetime.now(UTC) - started > timedelta(
        hours=2 * max(1, app_settings.RETENTION_SWEEP_INTERVAL_HOURS)
    )
    if overdue:
        return ComplianceCheckItem(
            id="data_retention",
            label=label,
            description=f"{period} The retention sweep last ran on {when} and is overdue.",
            status="warning",
        )
    return ComplianceCheckItem(
        id="data_retention",
        label=label,
        description=f"{period} Last retention sweep ran on {when}.",
        status="complete" if retention_days <= MAX_COMPLIANT_RETENTION_DAYS else "warning",
    )


# =============================================================================
# Compliance Status Endpoints
# =============================================================================


@router.get("/status", response_model=ComplianceOverviewResponse)
async def get_compliance_status(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ComplianceOverviewResponse:
    """Get overall compliance status for GDPR and CCPA."""
    privacy_settings = await get_or_create_privacy_settings(current_user.id, db)
    user_settings = await get_user_settings(current_user.id, db)

    # Check if user has API keys configured
    has_openai = bool(user_settings and user_settings.openai_api_key)
    has_telnyx = bool(user_settings and user_settings.telnyx_api_key)
    has_deepgram = bool(user_settings and user_settings.deepgram_api_key)
    has_elevenlabs = bool(user_settings and user_settings.elevenlabs_api_key)

    # Check consent records - get the MOST RECENT record for each type
    # and check if it was granted (not withdrawn)
    consent_result = await db.execute(
        select(ConsentRecord)
        .where(ConsentRecord.user_id == current_user.id)
        .where(ConsentRecord.consent_type == "data_processing")
        .order_by(ConsentRecord.created_at.desc())
        .limit(1)
    )
    latest_data_consent = consent_result.scalar_one_or_none()
    has_data_processing_consent = latest_data_consent is not None and latest_data_consent.granted

    recording_consent_result = await db.execute(
        select(ConsentRecord)
        .where(ConsentRecord.user_id == current_user.id)
        .where(ConsentRecord.consent_type == "call_recording")
        .order_by(ConsentRecord.created_at.desc())
        .limit(1)
    )
    latest_recording_consent = recording_consent_result.scalar_one_or_none()
    has_recording_consent = (
        latest_recording_consent is not None and latest_recording_consent.granted
    )

    # What was actually run, not what the code is able to do.
    last_export = await latest_run(db, KIND_EXPORT, user_id=current_user.id)
    last_erasure = await latest_run(db, KIND_ERASURE, user_id=current_user.id)
    last_sweep = await latest_run(db, KIND_RETENTION, trigger=TRIGGER_SCHEDULE)
    public_url = app_settings.PUBLIC_URL or ""
    served_over_tls = public_url.lower().startswith("https://")
    credentials_encrypted = get_box() is not None

    # GDPR Checks
    gdpr_checks: list[ComplianceCheckItem] = [
        ComplianceCheckItem(
            id="encryption_transit",
            label="Encryption in Transit",
            description="The public address is served over HTTPS/WSS"
            if served_over_tls
            else "No HTTPS public address is configured (PUBLIC_URL), so this cannot be confirmed",
            status="complete" if served_over_tls else "warning",
        ),
        ComplianceCheckItem(
            id="encryption_at_rest",
            label="Credentials Encrypted at Rest",
            description="Provider and integration credentials are stored encrypted"
            if credentials_encrypted
            else "No credential key is configured; credentials are stored in plain text",
            status="complete" if credentials_encrypted else "incomplete",
        ),
        ComplianceCheckItem(
            id="user_authentication",
            label="User Authentication",
            description="Secure JWT-based authentication enabled",
            status="complete",
        ),
        ComplianceCheckItem(
            id="password_hashing",
            label="Password Security",
            description="Passwords hashed with bcrypt",
            status="complete",
        ),
        ComplianceCheckItem(
            id="data_processing_consent",
            label="Data Processing Consent",
            description="User has consented to data processing",
            status="complete" if has_data_processing_consent else "incomplete",
            action_label="Record Consent" if not has_data_processing_consent else None,
        ),
        ComplianceCheckItem(
            id="recording_consent",
            label="Call Recording Consent",
            description="User has consented to call recording",
            status="complete" if has_recording_consent else "incomplete",
            action_label="Record Consent" if not has_recording_consent else None,
        ),
        ComplianceCheckItem(
            id="privacy_policy",
            label="Privacy Policy",
            description="Privacy policy URL configured",
            status="complete" if privacy_settings.privacy_policy_url else "incomplete",
            action_label="Add Policy URL" if not privacy_settings.privacy_policy_url else None,
        ),
        _retention_check(privacy_settings.data_retention_days, last_sweep),
        _run_check(
            "data_export",
            "Data Export",
            last_export,
            never="Export has not been run for this account yet",
            succeeded="Last export succeeded on",
            failed="Last export failed on",
        ),
        _run_check(
            "data_deletion",
            "Data Deletion",
            last_erasure,
            never="Deletion has not been run for this account yet",
            succeeded="Last deletion succeeded on",
            failed="Last deletion failed on",
        ),
        ComplianceCheckItem(
            id="consent_withdrawal",
            label="Consent Withdrawal",
            description="Users can withdraw consent at any time",
            status="complete",
        ),
        ComplianceCheckItem(
            id="automated_decisions",
            label="AI Decision Disclosure",
            description="Users informed about AI-driven voice agents",
            status="complete",
        ),
        ComplianceCheckItem(
            id="data_minimization",
            label="Data Minimization",
            description="Only necessary data is collected",
            status="warning",
            action_label="Review Data Collection",
        ),
    ]

    # Add DPA checks only for services being used
    if has_openai:
        gdpr_checks.append(
            ComplianceCheckItem(
                id="openai_dpa",
                label="OpenAI DPA",
                description="Data Processing Agreement with OpenAI",
                status="complete" if privacy_settings.openai_dpa_signed else "incomplete",
                action_url="https://openai.com/policies/data-processing-addendum",
                action_label="Sign DPA" if not privacy_settings.openai_dpa_signed else None,
            )
        )

    if has_telnyx:
        gdpr_checks.append(
            ComplianceCheckItem(
                id="telnyx_dpa",
                label="Telnyx DPA",
                description="Data Processing Agreement with Telnyx",
                status="complete" if privacy_settings.telnyx_dpa_signed else "incomplete",
                action_url="https://telnyx.com/legal/data-processing-addendum",
                action_label="Sign DPA" if not privacy_settings.telnyx_dpa_signed else None,
            )
        )

    if has_deepgram:
        gdpr_checks.append(
            ComplianceCheckItem(
                id="deepgram_dpa",
                label="Deepgram DPA",
                description="Data Processing Agreement with Deepgram",
                status="complete" if privacy_settings.deepgram_dpa_signed else "incomplete",
                action_url="https://developers.deepgram.com/docs/data-privacy-compliance",
                action_label="Sign DPA" if not privacy_settings.deepgram_dpa_signed else None,
            )
        )

    if has_elevenlabs:
        gdpr_checks.append(
            ComplianceCheckItem(
                id="elevenlabs_dpa",
                label="ElevenLabs DPA",
                description="Data Processing Agreement with ElevenLabs",
                status="complete" if privacy_settings.elevenlabs_dpa_signed else "incomplete",
                action_url="https://elevenlabs.io/dpa",
                action_label="Sign DPA" if not privacy_settings.elevenlabs_dpa_signed else None,
            )
        )

    gdpr_completed = sum(1 for c in gdpr_checks if c.status == "complete")
    gdpr_total = len(gdpr_checks)

    # CCPA Checks
    ccpa_checks: list[ComplianceCheckItem] = [
        ComplianceCheckItem(
            id="data_disclosure",
            label="Data Collection Disclosure",
            description="Categories of data collected are documented",
            status="complete" if privacy_settings.privacy_policy_url else "incomplete",
            action_label="Add Privacy Policy" if not privacy_settings.privacy_policy_url else None,
        ),
        ComplianceCheckItem(
            id="opt_out_right",
            label="Opt-Out Right",
            description="Users can opt out of data sale/sharing",
            status="complete",
        ),
        ComplianceCheckItem(
            id="opt_out_status",
            label="Opt-Out Status",
            description="Do Not Sell My Personal Information"
            if privacy_settings.ccpa_opt_out
            else "User has not opted out",
            status="complete" if privacy_settings.ccpa_opt_out else "warning",
        ),
        _run_check(
            "data_access",
            "Data Access Right",
            last_export,
            never="Export has not been run for this account yet",
            succeeded="Last export succeeded on",
            failed="Last export failed on",
        ),
        _run_check(
            "data_deletion_ccpa",
            "Deletion Right",
            last_erasure,
            never="Deletion has not been run for this account yet",
            succeeded="Last deletion succeeded on",
            failed="Last deletion failed on",
        ),
        ComplianceCheckItem(
            id="non_discrimination",
            label="Non-Discrimination",
            description="No discrimination for exercising privacy rights",
            status="complete",
        ),
    ]

    ccpa_completed = sum(1 for c in ccpa_checks if c.status == "complete")
    ccpa_total = len(ccpa_checks)

    return ComplianceOverviewResponse(
        gdpr=GDPRStatusResponse(
            completed=gdpr_completed,
            total=gdpr_total,
            percentage=int((gdpr_completed / gdpr_total) * 100) if gdpr_total > 0 else 0,
            checks=gdpr_checks,
        ),
        ccpa=CCPAStatusResponse(
            completed=ccpa_completed,
            total=ccpa_total,
            percentage=int((ccpa_completed / ccpa_total) * 100) if ccpa_total > 0 else 0,
            checks=ccpa_checks,
        ),
    )


# =============================================================================
# Privacy Settings Endpoints
# =============================================================================


@router.get("/privacy-settings", response_model=PrivacySettingsResponse)
async def get_privacy_settings(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrivacySettingsResponse:
    """Get user's privacy settings."""
    settings = await get_or_create_privacy_settings(current_user.id, db)

    return PrivacySettingsResponse(
        privacy_policy_url=settings.privacy_policy_url,
        privacy_policy_accepted_at=settings.privacy_policy_accepted_at,
        data_retention_days=settings.data_retention_days,
        openai_dpa_signed=settings.openai_dpa_signed,
        openai_dpa_signed_at=settings.openai_dpa_signed_at,
        telnyx_dpa_signed=settings.telnyx_dpa_signed,
        telnyx_dpa_signed_at=settings.telnyx_dpa_signed_at,
        deepgram_dpa_signed=settings.deepgram_dpa_signed,
        deepgram_dpa_signed_at=settings.deepgram_dpa_signed_at,
        elevenlabs_dpa_signed=settings.elevenlabs_dpa_signed,
        elevenlabs_dpa_signed_at=settings.elevenlabs_dpa_signed_at,
        ccpa_opt_out=settings.ccpa_opt_out,
        ccpa_opt_out_at=settings.ccpa_opt_out_at,
        last_data_export_at=settings.last_data_export_at,
    )


@router.patch("/privacy-settings", response_model=PrivacySettingsResponse)
async def update_privacy_settings(
    request: UpdatePrivacySettingsRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrivacySettingsResponse:
    """Update user's privacy settings."""
    settings = await get_or_create_privacy_settings(current_user.id, db)
    now = datetime.now(UTC)

    if request.privacy_policy_url is not None:
        settings.privacy_policy_url = request.privacy_policy_url
        if request.privacy_policy_url:
            settings.privacy_policy_accepted_at = now

    if request.data_retention_days is not None:
        settings.data_retention_days = request.data_retention_days

    if request.openai_dpa_signed is not None:
        settings.openai_dpa_signed = request.openai_dpa_signed
        settings.openai_dpa_signed_at = now if request.openai_dpa_signed else None

    if request.telnyx_dpa_signed is not None:
        settings.telnyx_dpa_signed = request.telnyx_dpa_signed
        settings.telnyx_dpa_signed_at = now if request.telnyx_dpa_signed else None

    if request.deepgram_dpa_signed is not None:
        settings.deepgram_dpa_signed = request.deepgram_dpa_signed
        settings.deepgram_dpa_signed_at = now if request.deepgram_dpa_signed else None

    if request.elevenlabs_dpa_signed is not None:
        settings.elevenlabs_dpa_signed = request.elevenlabs_dpa_signed
        settings.elevenlabs_dpa_signed_at = now if request.elevenlabs_dpa_signed else None

    if request.ccpa_opt_out is not None:
        settings.ccpa_opt_out = request.ccpa_opt_out
        settings.ccpa_opt_out_at = now if request.ccpa_opt_out else None

    await db.commit()
    await db.refresh(settings)

    logger.info("privacy_settings_updated", user_id=current_user.id)

    return PrivacySettingsResponse(
        privacy_policy_url=settings.privacy_policy_url,
        privacy_policy_accepted_at=settings.privacy_policy_accepted_at,
        data_retention_days=settings.data_retention_days,
        openai_dpa_signed=settings.openai_dpa_signed,
        openai_dpa_signed_at=settings.openai_dpa_signed_at,
        telnyx_dpa_signed=settings.telnyx_dpa_signed,
        telnyx_dpa_signed_at=settings.telnyx_dpa_signed_at,
        deepgram_dpa_signed=settings.deepgram_dpa_signed,
        deepgram_dpa_signed_at=settings.deepgram_dpa_signed_at,
        elevenlabs_dpa_signed=settings.elevenlabs_dpa_signed,
        elevenlabs_dpa_signed_at=settings.elevenlabs_dpa_signed_at,
        ccpa_opt_out=settings.ccpa_opt_out,
        ccpa_opt_out_at=settings.ccpa_opt_out_at,
        last_data_export_at=settings.last_data_export_at,
    )


# =============================================================================
# Consent Endpoints
# =============================================================================


@router.post("/consent")
async def record_consent(
    request: ConsentRequest,
    http_request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Record user consent for GDPR compliance."""
    consent = ConsentRecord(
        user_id=current_user.id,
        consent_type=request.consent_type,
        granted=request.granted,
        ip_address=http_request.client.host if http_request.client else None,
        user_agent=http_request.headers.get("user-agent"),
    )
    db.add(consent)
    await record_audit(
        db,
        AuditAction.CONSENT_UPDATE,
        actor_user_id=current_user.id,
        target_type="consent",
        target_id=request.consent_type,
        details={"granted": request.granted},
        request=http_request,
    )
    await db.commit()

    logger.info(
        "consent_recorded",
        user_id=current_user.id,
        consent_type=request.consent_type,
        granted=request.granted,
    )

    return {"message": f"Consent for {request.consent_type} recorded successfully"}


# =============================================================================
# Data Export Endpoint (GDPR Article 20 / CCPA)
# =============================================================================


@router.get("/export", response_model=DataExportResponse)
async def export_user_data(
    http_request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DataExportResponse:
    """Export all user data for GDPR/CCPA compliance.

    Each attempt is recorded, including one that fails, so the status page
    reports whether export works and not merely that the route exists.
    """
    user_id = current_user.id
    try:
        response = await _build_export(current_user, db)
        run = await start_run(db, KIND_EXPORT, trigger=TRIGGER_USER, user_id=user_id)
        counts = {
            "agents": len(response.agents),
            "workspaces": len(response.workspaces),
            "contacts": len(response.contacts),
            "appointments": len(response.appointments),
            "call_records": len(response.call_records),
            "call_interactions": len(response.call_interactions),
            "consent_records": len(response.consent_records),
            "phone_numbers": len(response.phone_numbers),
            "campaigns": len(response.campaigns),
            "integrations": len(response.integrations),
            "caller_audio": len(response.caller_audio),
        }
        finish_run(run, counts)
        await record_audit(
            db,
            AuditAction.DATA_EXPORT,
            actor_user_id=user_id,
            target_type="account",
            target_id=str(user_id),
            details={"exported": counts},
            request=http_request,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        await record_failed_run(db, KIND_EXPORT, trigger=TRIGGER_USER, user_id=user_id, error=exc)
        raise

    logger.info("data_exported", user_id=user_id)
    return response


async def _build_export(current_user: CurrentUser, db: AsyncSession) -> DataExportResponse:
    """Collect everything the account holds. Credentials are reported as set or not."""
    now = datetime.now(UTC)
    user_id = current_user.id
    user_uuid = user_id_to_uuid(user_id)

    user_data: dict[str, object] = {
        "id": user_id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
    }

    # User settings (never the keys themselves)
    user_settings = await get_user_settings(user_id, db)
    settings_data: dict[str, object] | None = None
    if user_settings:
        settings_data = {
            "openai_api_key_set": bool(user_settings.openai_api_key),
            "deepgram_api_key_set": bool(user_settings.deepgram_api_key),
            "elevenlabs_api_key_set": bool(user_settings.elevenlabs_api_key),
            "telnyx_api_key_set": bool(user_settings.telnyx_api_key),
            "twilio_account_sid_set": bool(user_settings.twilio_account_sid),
            "created_at": user_settings.created_at.isoformat(),
            "updated_at": user_settings.updated_at.isoformat(),
        }

    privacy_settings = await get_or_create_privacy_settings(user_id, db)
    privacy_data: dict[str, object] = {
        "privacy_policy_url": privacy_settings.privacy_policy_url,
        "data_retention_days": privacy_settings.data_retention_days,
        "ccpa_opt_out": privacy_settings.ccpa_opt_out,
        "openai_dpa_signed": privacy_settings.openai_dpa_signed,
        "telnyx_dpa_signed": privacy_settings.telnyx_dpa_signed,
        "deepgram_dpa_signed": privacy_settings.deepgram_dpa_signed,
        "elevenlabs_dpa_signed": privacy_settings.elevenlabs_dpa_signed,
    }

    # Agents and workspaces are keyed by the integer user id.
    agents_result = await db.execute(select(Agent).where(Agent.user_id == user_id))
    agents_data: list[dict[str, object]] = [
        {
            "id": str(a.id),
            "name": a.name,
            "description": a.description,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in agents_result.scalars().all()
    ]

    workspaces_result = await db.execute(select(Workspace).where(Workspace.user_id == user_id))
    workspaces_data: list[dict[str, object]] = [
        {
            "id": str(w.id),
            "name": w.name,
            "description": w.description,
            "created_at": w.created_at.isoformat() if w.created_at else None,
        }
        for w in workspaces_result.scalars().all()
    ]

    workspace_ids = await owned_workspace_ids(db, user_id)
    agent_ids = await owned_agent_ids(db, user_id)
    contact_filter = contact_owner_filter(user_id, workspace_ids)
    owned_contacts = select(Contact.id).where(contact_filter)

    contacts_result = await db.execute(select(Contact).where(contact_filter))
    contacts_data: list[dict[str, object]] = [
        {
            "id": str(c.id),
            "first_name": c.first_name,
            "last_name": c.last_name,
            "email": c.email,
            "phone_number": c.phone_number,
            "company_name": c.company_name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in contacts_result.scalars().all()
    ]

    appointments_result = await db.execute(
        select(Appointment).where(Appointment.contact_id.in_(owned_contacts))
    )
    appointments_data: list[dict[str, object]] = [
        {
            "id": str(a.id),
            "scheduled_at": a.scheduled_at.isoformat() if a.scheduled_at else None,
            "duration_minutes": a.duration_minutes,
            "status": a.status,
            "service_type": a.service_type,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in appointments_result.scalars().all()
    ]

    call_records_result = await db.execute(
        select(CallRecord).where(call_record_owner_filter(user_id, agent_ids, workspace_ids))
    )
    call_records_data: list[dict[str, object]] = [
        {
            "id": str(cr.id),
            "direction": cr.direction,
            "status": cr.status,
            "from_number": cr.from_number,
            "to_number": cr.to_number,
            "duration_seconds": cr.duration_seconds,
            "started_at": cr.started_at.isoformat() if cr.started_at else None,
            "ended_at": cr.ended_at.isoformat() if cr.ended_at else None,
        }
        for cr in call_records_result.scalars().all()
    ]

    interactions_result = await db.execute(
        select(CallInteraction).where(CallInteraction.contact_id.in_(owned_contacts))
    )
    call_interactions_data: list[dict[str, object]] = [
        {
            "id": str(ci.id),
            "agent_name": ci.agent_name,
            "outcome": ci.outcome,
            "duration_seconds": ci.duration_seconds,
            "sentiment_score": ci.sentiment_score,
            "call_started_at": (ci.call_started_at.isoformat() if ci.call_started_at else None),
        }
        for ci in interactions_result.scalars().all()
    ]

    consent_result = await db.execute(select(ConsentRecord).where(ConsentRecord.user_id == user_id))
    consent_data: list[dict[str, object]] = [
        {
            "id": str(cr.id),
            "consent_type": cr.consent_type,
            "granted": cr.granted,
            "created_at": cr.created_at.isoformat() if cr.created_at else None,
        }
        for cr in consent_result.scalars().all()
    ]

    # These three are keyed by the UUID derived from the user id.
    numbers_result = await db.execute(select(PhoneNumber).where(PhoneNumber.user_id == user_uuid))
    phone_numbers_data: list[dict[str, object]] = [
        {
            "id": str(n.id),
            "phone_number": n.phone_number,
            "provider": n.provider,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in numbers_result.scalars().all()
    ]

    campaigns_result = await db.execute(
        select(Campaign.id, Campaign.name, Campaign.status, Campaign.created_at).where(
            Campaign.user_id == user_uuid
        )
    )
    campaigns_data: list[dict[str, object]] = [
        {
            "id": str(row.id),
            "name": row.name,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in campaigns_result.all()
    ]

    integrations_result = await db.execute(
        select(
            UserIntegration.integration_id,
            UserIntegration.integration_name,
            UserIntegration.is_active,
            UserIntegration.created_at,
        ).where(UserIntegration.user_id == user_uuid)
    )
    integrations_data: list[dict[str, object]] = [
        {
            "integration_id": row.integration_id,
            "integration_name": row.integration_name,
            "is_active": row.is_active,
            "connected_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in integrations_result.all()
    ]

    privacy_settings.last_data_export_at = now
    privacy_settings.last_data_export_requested_at = now

    audio_result = await db.execute(
        select(CallerAudio, Workspace.audio_retention_days)
        .join(Workspace, Workspace.id == CallerAudio.workspace_id)
        .where(CallerAudio.workspace_id.in_(workspace_ids) if workspace_ids else false())
        .order_by(CallerAudio.created_at)
    )
    caller_audio_data: list[dict[str, object]] = [
        {
            "id": str(a.id),
            "workspace_id": str(a.workspace_id),
            "run_id": str(a.run_pk),
            "call_id": str(a.call_id),
            "track": a.track,
            "duration_s": a.duration_s,
            "size_bytes": a.size_bytes,
            "stored_at": a.created_at.isoformat() if a.created_at else None,
            "deleted_after": (
                (a.created_at + timedelta(days=days)).isoformat() if a.created_at else None
            ),
        }
        for a, days in audio_result.all()
    ]

    return DataExportResponse(
        user=user_data,
        settings=settings_data,
        privacy_settings=privacy_data,
        agents=agents_data,
        workspaces=workspaces_data,
        contacts=contacts_data,
        appointments=appointments_data,
        call_records=call_records_data,
        call_interactions=call_interactions_data,
        consent_records=consent_data,
        phone_numbers=phone_numbers_data,
        campaigns=campaigns_data,
        integrations=integrations_data,
        caller_audio=caller_audio_data,
        exported_at=now,
    )


# =============================================================================
# CCPA Opt-Out Endpoint
# =============================================================================


@router.post("/ccpa/opt-out")
async def ccpa_opt_out(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Opt out of data sale/sharing under CCPA."""
    settings = await get_or_create_privacy_settings(current_user.id, db)
    settings.ccpa_opt_out = True
    settings.ccpa_opt_out_at = datetime.now(UTC)
    await db.commit()

    logger.info("ccpa_opt_out", user_id=current_user.id)

    return {"message": "You have successfully opted out of data sale/sharing"}


@router.post("/ccpa/opt-in")
async def ccpa_opt_in(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Opt back in to data sharing under CCPA."""
    settings = await get_or_create_privacy_settings(current_user.id, db)
    settings.ccpa_opt_out = False
    settings.ccpa_opt_out_at = None
    await db.commit()

    logger.info("ccpa_opt_in", user_id=current_user.id)

    return {"message": "You have opted back in to data sharing"}


# =============================================================================
# Consent Withdrawal Endpoint
# =============================================================================


@router.post("/consent/withdraw")
async def withdraw_consent(
    request: ConsentRequest,
    http_request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Withdraw previously given consent (GDPR Article 7(3))."""
    # Record the withdrawal as a new consent record with granted=False
    consent = ConsentRecord(
        user_id=current_user.id,
        consent_type=request.consent_type,
        granted=False,
        ip_address=http_request.client.host if http_request.client else None,
        user_agent=http_request.headers.get("user-agent"),
    )
    db.add(consent)
    await record_audit(
        db,
        AuditAction.CONSENT_UPDATE,
        actor_user_id=current_user.id,
        target_type="consent",
        target_id=request.consent_type,
        details={"granted": False},
        request=http_request,
    )
    await db.commit()

    logger.info(
        "consent_withdrawn",
        user_id=current_user.id,
        consent_type=request.consent_type,
    )

    return {"message": f"Consent for {request.consent_type} has been withdrawn"}


# =============================================================================
# Data Deletion Endpoint (GDPR Article 17 / CCPA Right to Delete)
# =============================================================================


@router.delete("/data", response_model=DataDeletionResponse)
async def delete_user_data(
    http_request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DataDeletionResponse:
    """
    Delete all user data (Right to Erasure / Right to Delete).

    GDPR Article 17 - Right to erasure ('right to be forgotten')
    CCPA - Right to Delete

    Deletes agents, workspaces, contacts, appointments, call records and their
    evaluations, call interactions, campaigns, phone numbers, test scenarios
    and runs, integration credentials, provider keys, consent records and
    privacy settings.

    What stays: the login itself, the audit trail, and the record that this
    erasure ran. Each attempt is recorded, including one that fails.
    """
    user_id = current_user.id
    now = datetime.now(UTC)
    try:
        run = await start_run(db, KIND_ERASURE, trigger=TRIGGER_USER, user_id=user_id)
        not_erased: list[dict[str, str]] = []
        deleted_counts = await erase_account_data(db, user_id, not_erased)
        finish_run(run, deleted_counts)
        await record_audit(
            db,
            AuditAction.DATA_ERASURE,
            actor_user_id=user_id,
            target_type="account",
            target_id=str(user_id),
            details={"deleted": deleted_counts, "not_erased_here": not_erased},
            request=http_request,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        await record_failed_run(db, KIND_ERASURE, trigger=TRIGGER_USER, user_id=user_id, error=exc)
        raise

    logger.warning("user_data_deleted", user_id=user_id, deleted_counts=deleted_counts)

    return DataDeletionResponse(
        deleted_counts=deleted_counts,
        deleted_at=now,
        complete=not not_erased,
        not_erased_here=not_erased,
    )


# =============================================================================
# Data Retention
# =============================================================================


@router.post("/retention/cleanup")
async def trigger_retention_cleanup(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Apply the retention period to the caller's own data now.

    The scheduled sweep (app.services.data_protection) covers every account;
    this covers only the signed-in account, so one customer cannot trigger
    deletion in another's data.
    """
    run = await run_retention(db, trigger=TRIGGER_USER, user_id=current_user.id)
    return {
        "message": "Retention cleanup completed",
        "deleted_counts": run.counts or {},
    }
