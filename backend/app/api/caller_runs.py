"""Caller results API: upload a batch from the caller rig, then read it back.

The caller (``caller/``) is a command-line tool that places calls into a voice
agent and grades them. Until this module nothing in the application knew it
existed, so a result lived only in a directory on the machine that ran it.
``synthiq-caller upload`` posts a bundle here; the dashboard reads it from here.

Every route is scoped to the signed-in user's own workspaces. A run or a call in
somebody else's workspace is answered exactly like one that does not exist: 404.
"""

import asyncio
import hashlib
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.audit import AuditAction, record_audit
from app.core.auth import CurrentUser
from app.core.config import settings
from app.core.limiter import limiter
from app.db.session import get_db
from app.models.caller_audio import TRACKS, CallerAudio
from app.models.caller_run import CallerCall, CallerRun
from app.models.workspace import Workspace
from app.services.audio_store import (
    AudioRejected,
    AudioStore,
    RangeNotSatisfiable,
    get_audio_store,
    inspect_wav,
    parse_range,
)
from app.services.caller_audio import delete_audio, expire_audio
from app.services.caller_results import (
    GRADED_OUTCOMES,
    MAX_BUNDLE_BYTES,
    BundleError,
    ParsedBundle,
    failures_by_fault_class,
    field_tallies,
    outcome_counts,
    parse_bundle,
    rate,
    scenario_table,
)

router = APIRouter(prefix="/api/v1/caller", tags=["caller"])
logger = structlog.get_logger()

NOT_FOUND = "Caller run not found"


# =============================================================================
# Response models
# =============================================================================


class Rate(BaseModel):
    """A pass count over a total. ``rate`` and the interval are null when total is 0:
    "no calls to rate" is a different fact from "none passed"."""

    passed: int
    total: int
    rate: float | None
    interval_low: float | None
    interval_high: float | None


class RunSummary(BaseModel):
    id: str
    run_id: str
    workspace_id: str
    workspace_name: str | None
    target_label: str
    rig_version: str
    scenario_set_hash: str
    payload_version: int
    started_at: datetime | None
    ended_at: datetime | None
    uploaded_at: datetime
    attempted: int
    graded: int
    passed: int
    failed: int
    not_graded: int
    not_run: int
    excluded_count: int
    # Calls by outcome as written (passed, failed, void, ungradeable, ungraded, ...),
    # with `synthetic_calls` and `labels_unverified_calls` beside them.
    outcome_counts: dict[str, int]
    of_graded: Rate
    of_attempted: Rate


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    total: int
    page: int
    page_size: int
    total_pages: int


class UploadResponse(BaseModel):
    # "created", "unchanged" (same run id, same content) or "replaced".
    status: str
    run: RunSummary


class CallRow(BaseModel):
    id: str
    position: int
    scenario: str
    outcome: str
    fault_class: str | None
    last_step: str
    duration_s: float | None
    synthetic: bool
    labels_unverified: bool


class RunDetail(RunSummary):
    scenarios: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    chains: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    calls: list[CallRow]
    # Whatever summary the caller itself sent, untouched. Null when it sent none.
    reported_summary: dict[str, Any] | None


class AudioRef(BaseModel):
    """One recorded side of a call. ``url`` is relative to the host and needs the same auth."""

    track: str
    url: str
    duration_s: float
    size_bytes: int
    sample_rate: int


class AudioUploadResponse(BaseModel):
    # "stored", or "replaced" when this side of this call already had a recording.
    status: str
    audio: AudioRef


class CallDetail(BaseModel):
    id: str
    run: dict[str, Any]
    position: int
    file: str
    scenario: str
    scenario_file: str
    mutates: str
    repeat: int | None
    outcome: str
    fault_class: str | None
    last_step: str
    conversation_id: str | None
    ended: str
    duration_s: float | None
    excluded_reason: str | None
    fields: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    said: list[dict[str, Any]]
    step_path: list[str]
    # None: no read-back was made. An object: what the system held afterwards.
    read_back: dict[str, Any] | None
    # Clips in this call spoken by a synthetic voice, and clips whose label no
    # person has confirmed. Both empty for a call of confirmed human recordings.
    synthetic_clips: list[str]
    labels_unverified: list[str]
    notes: dict[str, str]
    # The recorded sides of this call that are still stored; empty when there are none.
    audio: list[AudioRef]
    # True when the call had recordings and retention has deleted them.
    audio_expired: bool = False


# =============================================================================
# Helpers
# =============================================================================


def _summary(run: CallerRun, workspace_name: str | None) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "run_id": run.run_id,
        "workspace_id": str(run.workspace_id),
        "workspace_name": workspace_name,
        "target_label": run.target_label,
        "rig_version": run.rig_version,
        "scenario_set_hash": run.scenario_set_hash,
        "payload_version": run.payload_version,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "uploaded_at": run.created_at,
        "attempted": run.attempted,
        "graded": run.graded,
        "passed": run.passed,
        "failed": run.failed,
        "not_graded": run.attempted - run.graded,
        "not_run": run.not_run,
        "excluded_count": len(run.excluded or []),
        "outcome_counts": run.outcome_counts or {},
        "of_graded": rate(run.passed, run.graded),
        "of_attempted": rate(run.passed, run.attempted),
    }


def _names(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


async def _owned_workspace(db: AsyncSession, workspace_id: uuid.UUID, user_id: int) -> Workspace:
    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.user_id == user_id)
    )
    workspace = result.scalar_one_or_none()
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


async def _owned_run(
    db: AsyncSession, run_pk: uuid.UUID, user_id: int, *, with_raw: bool = False
) -> tuple[CallerRun, str]:
    """The run and its workspace's name, only when the workspace is the user's own."""
    query = (
        select(CallerRun, Workspace.name)
        .join(Workspace, Workspace.id == CallerRun.workspace_id)
        .where(CallerRun.id == run_pk, Workspace.user_id == user_id)
    )
    if with_raw:
        query = query.options(undefer(CallerRun.raw))
    row = (await db.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return row[0], row[1]


async def _read_capped(request: Request, limit: int | None = None, what: str = "bundle") -> bytes:
    """The request body, refused as soon as it is known to be over the cap."""
    limit = MAX_BUNDLE_BYTES if limit is None else limit
    too_large = HTTPException(
        status_code=413,
        detail=f"The {what} is larger than {limit // (1024 * 1024)} MB",
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def _audio_ref(run_pk: uuid.UUID, call_id: uuid.UUID, row: CallerAudio) -> dict[str, Any]:
    return {
        "track": row.track,
        "url": f"/api/v1/caller/runs/{run_pk}/calls/{call_id}/audio/{row.track}",
        "duration_s": row.duration_s,
        "size_bytes": row.size_bytes,
        "sample_rate": row.sample_rate,
    }


async def _delete_run_audio(db: AsyncSession, store: AudioStore, run_pk: uuid.UUID) -> None:
    """Delete a run's audio files and rows, or refuse: never leave files behind unsaid."""
    _, failed = await delete_audio(db, store, CallerAudio.run_pk == run_pk)
    if failed:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"{len(failed)} audio file(s) could not be deleted, so the run was kept. "
                "Check the audio directory's permissions and try again."
            ),
        )


def _rows(bundle: ParsedBundle, workspace_id: uuid.UUID, user_id: int) -> CallerRun:
    """The run and its calls as unsaved rows, with the counts worked out here.

    The counts come from the calls, never from a number the uploader sent: a
    headline that disagrees with its own rows is worse than no headline.
    """
    run_pk = uuid.uuid4()
    calls = [
        CallerCall(
            id=uuid.uuid4(),
            run_pk=run_pk,
            workspace_id=workspace_id,
            position=call.position,
            file=call.file,
            scenario=call.scenario,
            scenario_file=call.scenario_file,
            mutates=call.mutates,
            repeat=call.repeat,
            outcome=call.outcome,
            fault_class=call.fault_class,
            last_step=call.last_step,
            conversation_id=call.conversation_id,
            ended=call.ended,
            duration_s=call.duration_s,
            synthetic=call.synthetic,
            labels_unverified=call.labels_unverified,
            fields=call.fields,
            checks=call.checks,
            timeline=call.timeline,
            tool_calls=call.tool_calls,
            raw=call.raw,
        )
        for call in bundle.calls
    ]
    excluded: list[dict[str, Any]] = [
        {
            "kind": "not graded",
            "call_id": str(row.id),
            "scenario": row.scenario,
            "scenario_file": row.scenario_file,
            "repeat": row.repeat,
            "outcome": row.outcome,
            "fault": row.fault_class or "",
            "reason": parsed.exclusion_reason,
        }
        for row, parsed in zip(calls, bundle.calls, strict=True)
        if parsed.outcome not in GRADED_OUTCOMES
    ]
    excluded += [
        {
            "kind": "not run",
            "call_id": None,
            "scenario": str(item.get("scenario", "")),
            "scenario_file": str(item.get("scenario_file", "")),
            "repeat": item.get("repeat") if isinstance(item.get("repeat"), int) else None,
            "outcome": "not run",
            "fault": "",
            "reason": str(item.get("reason", "")),
        }
        for item in bundle.not_run
    ]
    run = CallerRun(
        id=run_pk,
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=bundle.run_id,
        content_hash=bundle.content_hash,
        payload_version=bundle.payload_version,
        rig_version=bundle.rig_version,
        target_label=bundle.target_label,
        scenario_set_hash=bundle.scenario_set_hash,
        started_at=bundle.started_at,
        ended_at=bundle.ended_at,
        attempted=len(calls),
        graded=bundle.passed + bundle.failed,
        passed=bundle.passed,
        failed=bundle.failed,
        not_run=len(bundle.not_run),
        outcome_counts=outcome_counts(bundle.calls),
        excluded=excluded,
        chains=bundle.chains,
        raw=bundle.raw,
    )
    run.calls = calls
    return run


# =============================================================================
# Routes
# =============================================================================


@router.post("/runs", response_model=UploadResponse)
@limiter.limit("30/minute")
async def upload_run(
    request: Request,  # named `request` for slowapi, and read as a stream for the size cap
    response: Response,
    current_user: CurrentUser,
    workspace_id: uuid.UUID = Query(description="Workspace the run belongs to"),
    replace: bool = Query(
        default=False,
        description="Replace a stored run that has the same run id but different content",
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Store one caller bundle. Idempotent on the bundle's run id within a workspace.

    Posting the same bundle twice stores it once and answers 200 "unchanged".
    Posting different content under a stored run id is refused with 409 unless
    ``replace=true``, so a regraded batch cannot silently overwrite the result
    somebody already read.
    """
    workspace = await _owned_workspace(db, workspace_id, current_user.id)
    body = await _read_capped(request)
    try:
        bundle = parse_bundle(body)
    except BundleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    log = logger.bind(user_id=current_user.id, workspace_id=str(workspace_id), run_id=bundle.run_id)
    existing = (
        await db.execute(
            select(CallerRun).where(
                CallerRun.workspace_id == workspace_id, CallerRun.run_id == bundle.run_id
            )
        )
    ).scalar_one_or_none()

    outcome = "created"
    if existing is not None:
        if existing.content_hash == bundle.content_hash:
            return {"status": "unchanged", "run": _summary(existing, workspace.name)}
        if not replace:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Run {bundle.run_id!r} is already stored in this workspace with different "
                    "content. Upload with replace=true to overwrite it."
                ),
            )
        # The calls are replaced, so their audio goes with them; the uploader sends it again.
        await _delete_run_audio(db, get_audio_store(), existing.id)
        await db.execute(delete(CallerCall).where(CallerCall.run_pk == existing.id))
        await db.execute(delete(CallerRun).where(CallerRun.id == existing.id))
        outcome = "replaced"

    run = _rows(bundle, workspace_id, current_user.id)
    db.add(run)
    await record_audit(
        db,
        AuditAction.CALLER_RUN_UPLOAD,
        actor_user_id=current_user.id,
        workspace_id=workspace_id,
        target_type="caller_run",
        target_id=str(run.id),
        details={
            "run_id": bundle.run_id,
            "outcome": outcome,
            "calls": run.attempted,
            "content_hash": bundle.content_hash,
        },
        request=request,
    )
    try:
        await db.commit()
    except IntegrityError as error:
        # Two uploads of one run id at the same moment: the constraint decided.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {bundle.run_id!r} was stored by another upload at the same time.",
        ) from error
    await db.refresh(run)
    log.info("caller_run_stored", outcome=outcome, calls=run.attempted)
    if outcome == "created":
        response.status_code = status.HTTP_201_CREATED
    return {"status": outcome, "run": _summary(run, workspace.name)}


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: uuid.UUID | None = Query(default=None, description="Only this workspace"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """The signed-in user's runs, newest first."""
    owned = [Workspace.user_id == current_user.id]
    if workspace_id is not None:
        owned.append(CallerRun.workspace_id == workspace_id)

    total = (
        await db.execute(
            select(func.count(CallerRun.id))
            .join(Workspace, Workspace.id == CallerRun.workspace_id)
            .where(*owned)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(CallerRun, Workspace.name)
            .join(Workspace, Workspace.id == CallerRun.workspace_id)
            .where(*owned)
            .order_by(desc(CallerRun.created_at), desc(CallerRun.id))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return {
        "runs": [_summary(run, name) for run, name in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/runs/{run_pk}", response_model=RunDetail)
async def get_run(
    run_pk: uuid.UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One run: its headline, each scenario, failures by fault class, and what was left out."""
    run, workspace_name = await _owned_run(db, run_pk, current_user.id, with_raw=True)
    calls = list(
        (
            await db.execute(
                select(CallerCall).where(CallerCall.run_pk == run.id).order_by(CallerCall.position)
            )
        )
        .scalars()
        .all()
    )
    reported = (run.raw or {}).get("summary")
    return {
        **_summary(run, workspace_name),
        "scenarios": scenario_table(calls),
        "failures": failures_by_fault_class(calls),
        "excluded": run.excluded or [],
        "chains": run.chains or [],
        "fields": field_tallies(calls),
        "calls": [
            {
                "id": str(call.id),
                "position": call.position,
                "scenario": call.scenario,
                "outcome": call.outcome,
                "fault_class": call.fault_class,
                "last_step": call.last_step,
                "duration_s": call.duration_s,
                "synthetic": bool(call.synthetic),
                "labels_unverified": bool(call.labels_unverified),
            }
            for call in calls
        ],
        "reported_summary": reported if isinstance(reported, dict) else None,
    }


@router.get("/runs/{run_pk}/calls/{call_id}", response_model=CallDetail)
async def get_call(
    run_pk: uuid.UUID,
    call_id: uuid.UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One call: what the agent said, what the caller said, what was heard and stored."""
    run, _ = await _owned_run(db, run_pk, current_user.id)
    call = (
        await db.execute(
            select(CallerCall)
            .where(CallerCall.id == call_id, CallerCall.run_pk == run.id)
            .options(
                undefer(CallerCall.timeline),
                undefer(CallerCall.tool_calls),
                undefer(CallerCall.raw),
            )
        )
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Caller call not found")

    raw = call.raw or {}
    raw_record, raw_trace = raw.get("record"), raw.get("trace")
    record: dict[str, Any] = raw_record if isinstance(raw_record, dict) else {}
    trace: dict[str, Any] = raw_trace if isinstance(raw_trace, dict) else {}
    said = record.get("said")
    path = trace.get("step_path")
    read_back = raw.get("read_back")
    excluded = next(
        (item for item in (run.excluded or []) if item.get("call_id") == str(call.id)), None
    )
    # Expire this workspace's due audio first, so a recording past its period is
    # never listed or served in the gap before the periodic pass runs.
    await expire_audio(db, get_audio_store(), workspace_ids=[run.workspace_id])
    await db.commit()
    stored = (
        (
            await db.execute(
                select(CallerAudio)
                .where(CallerAudio.call_id == call.id)
                .order_by(CallerAudio.track)
            )
        )
        .scalars()
        .all()
    )
    recordings = [r for r in stored if r.expired_at is None]
    audio_expired = bool(stored) and not recordings
    return {
        "id": str(call.id),
        "run": {"id": str(run.id), "run_id": run.run_id, "target_label": run.target_label},
        "position": call.position,
        "file": call.file,
        "scenario": call.scenario,
        "scenario_file": call.scenario_file,
        "mutates": call.mutates,
        "repeat": call.repeat,
        "outcome": call.outcome,
        "fault_class": call.fault_class,
        "last_step": call.last_step,
        "conversation_id": call.conversation_id,
        "ended": call.ended,
        "duration_s": call.duration_s,
        "excluded_reason": str(excluded.get("reason", "")) if excluded else None,
        "fields": call.fields or [],
        "checks": call.checks or [],
        "timeline": call.timeline or [],
        "tool_calls": call.tool_calls or [],
        "said": [item for item in said if isinstance(item, dict)] if isinstance(said, list) else [],
        "step_path": [str(step) for step in path] if isinstance(path, list) else [],
        "read_back": read_back if isinstance(read_back, dict) else None,
        "synthetic_clips": _names(raw.get("synthetic_clips")),
        "labels_unverified": _names(raw.get("labels_unverified")),
        "notes": {
            key: raw[key]
            for key in ("trace_error", "read_back_error", "hook_error")
            if isinstance(raw.get(key), str) and raw[key]
        },
        "audio": [_audio_ref(run.id, call.id, row) for row in recordings],
        "audio_expired": audio_expired,
    }


@router.put("/runs/{run_pk}/audio", response_model=AudioUploadResponse)
@limiter.limit("120/minute")
async def upload_audio(
    request: Request,  # named `request` for slowapi; the body is read as a stream for the cap
    current_user: CurrentUser,
    run_pk: uuid.UUID,
    file: str = Query(max_length=200, description="The call's run file name, e.g. run-001.json"),
    track: str = Query(description="'caller' (what the rig played) or 'agent'"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Store one side of one call as a WAV file. A separate request per file.

    The run and the call must already be stored (upload the run first). The file
    is judged by its bytes, never by the Content-Type header, and is kept under a
    name this server generates. Sending the same side again replaces it.
    """
    run, _ = await _owned_run(db, run_pk, current_user.id)
    if track not in TRACKS:
        raise HTTPException(status_code=422, detail=f"track must be one of {list(TRACKS)}")
    call = (
        await db.execute(
            select(CallerCall).where(CallerCall.run_pk == run.id, CallerCall.file == file)
        )
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Caller call not found")
    data = await _read_capped(request, settings.CALLER_AUDIO_MAX_BYTES, "audio file")
    try:
        info = inspect_wav(data)
    except AudioRejected as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    store = get_audio_store()
    key = await asyncio.to_thread(store.put, run.workspace_id, data)
    existing = (
        await db.execute(
            select(CallerAudio).where(CallerAudio.call_id == call.id, CallerAudio.track == track)
        )
    ).scalar_one_or_none()
    old_key = existing.storage_key if existing is not None else None
    row = existing or CallerAudio(
        id=uuid.uuid4(),
        workspace_id=run.workspace_id,
        run_pk=run.id,
        call_id=call.id,
        track=track,
    )
    row.storage_key = key
    row.size_bytes = len(data)
    row.duration_s = round(info.duration_s, 3)
    row.sample_rate = info.sample_rate
    row.channels = info.channels
    row.sha256 = hashlib.sha256(data).hexdigest()
    row.expired_at = None
    db.add(row)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        store.delete(key)  # nothing points at it
        raise
    if old_key is not None:
        store.delete(old_key)
    logger.info("caller_audio_stored", user_id=current_user.id, track=track, bytes=len(data))
    return {
        "status": "replaced" if existing is not None else "stored",
        "audio": _audio_ref(run.id, call.id, row),
    }


@router.get("/runs/{run_pk}/calls/{call_id}/audio/{track}")
async def get_audio(
    request: Request,
    run_pk: uuid.UUID,
    call_id: uuid.UUID,
    track: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """One recorded side of a call, with HTTP Range support so a player can seek.

    A run in another user's workspace is answered exactly like audio that does
    not exist: 404.
    """
    run, _ = await _owned_run(db, run_pk, current_user.id)
    await expire_audio(db, get_audio_store(), workspace_ids=[run.workspace_id])
    await db.commit()
    row = (
        await db.execute(
            select(CallerAudio).where(
                CallerAudio.run_pk == run.id,
                CallerAudio.call_id == call_id,
                CallerAudio.track == track,
                CallerAudio.expired_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Audio not found")
    store = get_audio_store()
    try:
        size = store.size(row.storage_key)
    except OSError as error:
        raise HTTPException(status_code=404, detail="Audio is no longer stored") from error
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        wanted = parse_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    start, end = wanted if wanted is not None else (0, size - 1)
    headers["Content-Length"] = str(end - start + 1)
    if wanted is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        store.read(row.storage_key, start, end),
        status_code=206 if wanted is not None else 200,
        media_type="audio/wav",
        headers=headers,
    )


@router.delete("/runs/{run_pk}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_run(
    request: Request,  # named `request` for slowapi; also gives the audit trail the address
    run_pk: uuid.UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a run, every call in it and the audio of those calls."""
    run, _ = await _owned_run(db, run_pk, current_user.id)
    await _delete_run_audio(db, get_audio_store(), run.id)
    await db.execute(delete(CallerCall).where(CallerCall.run_pk == run.id))
    await db.execute(delete(CallerRun).where(CallerRun.id == run.id))
    await record_audit(
        db,
        AuditAction.CALLER_RUN_DELETE,
        actor_user_id=current_user.id,
        workspace_id=run.workspace_id,
        target_type="caller_run",
        target_id=str(run.id),
        details={"run_id": run.run_id, "calls": run.attempted},
        request=request,
    )
    await db.commit()
    logger.info("caller_run_deleted", user_id=current_user.id, run_id=run.run_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
