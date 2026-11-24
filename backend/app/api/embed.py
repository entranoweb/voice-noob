"""Public embed API routes for embeddable voice widgets.

These endpoints are public by design: an anonymous visitor on a customer's site
talks to that customer's agent. What protects the agent's owner
(``app.core.embed_session`` has the reasoning):

- the embedding site is checked against the agent's allowed domains before a
  session is minted, and an agent with no allowed domains can be embedded by no
  other site;
- every call after that carries a signed, short-lived session token bound to the
  agent and to the site it was minted for;
- each endpoint is rate limited, per address and per session;
- a session has a hard maximum length;
- tools run only when the agent's owner has allowed them for embedded sessions.
"""

import asyncio
import contextlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.integrations import get_workspace_integrations
from app.core.auth import user_id_to_uuid
from app.core.config import settings
from app.core.embed_session import (
    EMBED_SESSION_EXPIRED,
    EMBED_SESSION_REJECTED,
    EmbedSessionCheck,
    embedding_site,
    mint_embed_session,
    session_rate_key,
    site_allowed,
    tools_allowed,
    verify_embed_session,
)
from app.core.limiter import limiter
from app.core.stream_token_use import claim_stream_token
from app.db.session import get_db
from app.models.agent import Agent
from app.models.workspace import Workspace
from app.services.gpt_realtime import GPTRealtimeSession

router = APIRouter(prefix="/api/public/embed", tags=["public-embed"])
ws_router = APIRouter(prefix="/ws/public/embed", tags=["public-embed-ws"])
logger = structlog.get_logger()


def _session_key(request: Request) -> str:
    """Rate-limit key for the session a request presents (never the token itself)."""
    return session_rate_key(request.headers.get("x-embed-session"))


class EmbedConfigResponse(BaseModel):
    """Public agent configuration for widget initialization."""

    public_id: str
    name: str
    greeting_message: str
    button_text: str
    theme: str
    position: str
    primary_color: str
    language: str
    voice: str


class EmbedSessionResponse(BaseModel):
    """Response for session creation."""

    session_id: str
    expires_at: str
    websocket_url: str


def _require_allowed_site(
    origin: str | None, parent_origin: str | None, agent: Agent, log: Any
) -> str:
    """The site this request is embedded in, or 403 if it may not embed the agent."""
    site = embedding_site(origin, parent_origin)
    if not site_allowed(site, agent.allowed_domains):
        log.warning("origin_not_allowed", site=site, allowed=agent.allowed_domains)
        raise HTTPException(status_code=403, detail="Origin not allowed")
    return site


def _require_session(
    token: str | None, origin: str | None, parent_origin: str | None, agent: Agent, log: Any
) -> EmbedSessionCheck:
    """The live session this request belongs to, or 401.

    The site is recomputed from this request and is part of what the token
    signs, so a token minted for one site is refused from another without a
    second allow-list lookup.
    """
    site = embedding_site(origin, parent_origin)
    check = verify_embed_session(token, agent_id=str(agent.id), site=site)
    if not check.ok:
        log.warning("embed_session_rejected", reason=check.verdict.value, site=site)
        raise HTTPException(status_code=401, detail="Embed session missing, expired or invalid")
    return check


async def get_agent_by_public_id(
    public_id: str,
    db: AsyncSession,
) -> Agent | None:
    """Get agent by public ID."""
    result = await db.execute(select(Agent).where(Agent.public_id == public_id))
    return result.scalar_one_or_none()


@router.get("/{public_id}/config", response_model=EmbedConfigResponse)
@limiter.limit("60/minute")
async def get_embed_config(
    public_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    origin: str | None = Header(None),
    x_embed_parent_origin: str | None = Header(None),
) -> EmbedConfigResponse:
    """Get public agent configuration for widget initialization.

    This endpoint returns only the information needed to render the widget.
    It does NOT expose sensitive data like system prompts or API keys.
    """
    log = logger.bind(endpoint="embed_config", public_id=public_id, origin=origin)

    # Get agent
    agent = await get_agent_by_public_id(public_id, db)
    if not agent:
        log.warning("agent_not_found")
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.embed_enabled:
        log.warning("embed_disabled")
        raise HTTPException(status_code=403, detail="Embedding is disabled for this agent")

    if not agent.is_active:
        log.warning("agent_inactive")
        raise HTTPException(status_code=403, detail="Agent is not active")

    _require_allowed_site(origin, x_embed_parent_origin, agent, log)

    # Extract embed settings with defaults
    embed_settings = agent.embed_settings or {}

    log.info("config_returned")

    return EmbedConfigResponse(
        public_id=public_id,
        name=agent.name,
        greeting_message=embed_settings.get("greeting_message", "Hi! How can I help you today?"),
        button_text=embed_settings.get("button_text", "Talk to us"),
        theme=embed_settings.get("theme", "auto"),
        position=embed_settings.get("position", "bottom-right"),
        primary_color=embed_settings.get("primary_color", "#6366f1"),
        language=agent.language,
        voice=agent.voice,
    )


@router.post("/{public_id}/session", response_model=EmbedSessionResponse)
@limiter.limit("10/minute")
async def create_embed_session(
    public_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    origin: str | None = Header(None),
    x_embed_parent_origin: str | None = Header(None),
) -> EmbedSessionResponse:
    """Create an ephemeral session for WebRTC/WebSocket connection.

    This creates a short-lived session token that can be used to
    establish a voice connection. The token expires after 5 minutes.
    """
    log = logger.bind(endpoint="embed_session", public_id=public_id, origin=origin)

    # Get agent
    agent = await get_agent_by_public_id(public_id, db)
    if not agent:
        log.warning("agent_not_found")
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.embed_enabled:
        log.warning("embed_disabled")
        raise HTTPException(status_code=403, detail="Embedding is disabled for this agent")

    if not agent.is_active:
        log.warning("agent_inactive")
        raise HTTPException(status_code=403, detail="Agent is not active")

    site = _require_allowed_site(origin, x_embed_parent_origin, agent, log)

    # A signed session, bound to this agent and to the site it is embedded in.
    # Nothing is stored: the token is its own proof, so it works the same on
    # every worker and survives a restart.
    session = mint_embed_session(agent_id=str(agent.id), site=site)
    expires_at = datetime.now(UTC) + timedelta(seconds=session.expires_in)
    ws_url = f"/ws/public/embed/{public_id}?session={session.token}"

    log.info("session_created", session_id=session.session_id[:8], site=site)

    return EmbedSessionResponse(
        session_id=session.token,
        expires_at=expires_at.isoformat(),
        websocket_url=ws_url,
    )


async def _get_embed_agent_context(
    websocket: WebSocket,
    public_id: str,
    db: AsyncSession,
    log: Any,
) -> tuple[Agent, uuid.UUID, int] | None:
    """Load the agent and its workspace for an already-authenticated embed socket.

    Returns (agent, workspace_id, user_id_int) or None once the socket is closed.
    """
    from app.models.workspace import AgentWorkspace

    agent = await get_agent_by_public_id(public_id, db)
    if not agent or not agent.embed_enabled or not agent.is_active:
        await websocket.send_json({"type": "error", "error": "Agent not available"})
        await websocket.close(code=4003)
        return None

    log.info("agent_loaded", agent_name=agent.name, tier=agent.pricing_tier)

    workspace_result = await db.execute(
        select(AgentWorkspace).where(AgentWorkspace.agent_id == agent.id).limit(1)
    )
    agent_workspace = workspace_result.scalar_one_or_none()

    if not agent_workspace:
        log.warning("no_workspace_for_agent")
        await websocket.send_json({"type": "error", "error": "Agent not configured properly"})
        await websocket.close(code=4000)
        return None

    return agent, agent_workspace.workspace_id, agent.user_id


async def _admit_embed_socket(
    websocket: WebSocket, public_id: str, session: str | None, db: AsyncSession, log: Any
) -> int | None:
    """Decide, before the socket is accepted, whether this session may connect.

    Returns the session's expiry (unix seconds) or None after refusing. Refusal
    happens on the handshake, so an unauthenticated client never holds an open
    socket and no model session is ever started for it.
    """
    agent = await get_agent_by_public_id(public_id, db)
    if not agent or not agent.embed_enabled or not agent.is_active:
        await websocket.close(code=4004)
        return None

    site = embedding_site(
        websocket.headers.get("origin"), websocket.query_params.get("parent_origin")
    )
    check = verify_embed_session(session, agent_id=str(agent.id), site=site)
    if not check.ok:
        log.warning("embed_socket_session_rejected", reason=check.verdict.value, site=site)
        await websocket.close(code=EMBED_SESSION_REJECTED)
        return None

    # One socket per session: without this a single minted session opens as many
    # model sessions as its holder cares to, each billed to the agent's owner.
    claim = await claim_stream_token(
        session or "", ttl_seconds=int(settings.EMBED_SESSION_MAX_SECONDS)
    )
    if not claim.ok:
        log.warning("embed_socket_session_rejected", reason=claim.value, site=site)
        await websocket.close(code=EMBED_SESSION_REJECTED)
        return None

    return check.expires_at


@ws_router.websocket("/{public_id}")
async def embed_websocket(
    websocket: WebSocket,
    public_id: str,
    session: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Public WebSocket endpoint for embed voice streaming."""
    ws_session_id = str(uuid.uuid4())
    log = logger.bind(endpoint="embed_websocket", public_id=public_id, ws_session_id=ws_session_id)

    expires_at = await _admit_embed_socket(websocket, public_id, session, db, log)
    if expires_at is None:
        return

    await websocket.accept()
    log.info("websocket_connected")

    try:
        context = await _get_embed_agent_context(websocket, public_id, db, log)
        if not context:
            return

        agent, workspace_id, user_id_int = context
        agent_config = {
            "system_prompt": agent.system_prompt,
            # No tool definitions reach the model unless the owner allowed tools
            # for embedded sessions, so there is nothing for it to call.
            "enabled_tools": agent.enabled_tools if tools_allowed(agent.embed_settings) else [],
            "language": agent.language,
            "voice": agent.voice or "shimmer",
        }

        if agent.pricing_tier in ("premium", "premium-mini"):
            async with GPTRealtimeSession(
                db=db,
                user_id=user_id_int,
                agent_config=agent_config,
                session_id=ws_session_id,
                workspace_id=workspace_id,
            ) as realtime_session:
                await websocket.send_json(
                    {
                        "type": "session.ready",
                        "session_id": ws_session_id,
                        "agent": {"name": agent.name, "voice": agent.voice},
                    }
                )
                # The session ends when its token does, whatever either side is
                # doing: this is the hard cap on what one session can cost.
                remaining = max(1.0, expires_at - time.time())
                try:
                    async with asyncio.timeout(remaining):
                        await _bridge_embed_streams(websocket, realtime_session, log)
                except TimeoutError:
                    log.info("embed_session_max_length_reached")
                    with contextlib.suppress(Exception):
                        await websocket.close(
                            code=EMBED_SESSION_EXPIRED, reason="Session time limit reached"
                        )
        else:
            await websocket.send_json(
                {"type": "error", "error": "Voice chat requires Premium tier agent"}
            )
            await websocket.close(code=4002)

    except WebSocketDisconnect:
        log.info("websocket_disconnected")
    except Exception as e:
        log.exception("websocket_error", error=str(e))
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "error": "Internal error"})
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
        log.info("websocket_closed")


async def _bridge_embed_streams(
    client_ws: WebSocket,
    realtime_session: GPTRealtimeSession,
    logger: Any,
) -> None:
    """Bridge audio streams between embed client and GPT Realtime."""

    async def client_to_realtime() -> None:
        """Forward messages from client to GPT Realtime."""
        try:
            while True:
                message = await client_ws.receive()

                if message["type"] == "websocket.disconnect":
                    logger.info("client_initiated_disconnect")
                    break

                if message["type"] == "websocket.receive":
                    if "bytes" in message:
                        await realtime_session.send_audio(message["bytes"])
                    elif "text" in message:
                        try:
                            data = json.loads(message["text"])
                            logger.debug("client_event", event_type=data.get("type"))
                        except json.JSONDecodeError:
                            continue

        except WebSocketDisconnect:
            logger.info("client_disconnected")
        except Exception as e:
            logger.exception("client_to_realtime_error", error=str(e))

    async def realtime_to_client() -> None:
        """Forward messages from GPT Realtime to client."""
        try:
            if not realtime_session.connection:
                logger.error("no_realtime_connection")
                return

            logger.info("starting_realtime_to_client_loop")
            async for event in realtime_session.connection:
                try:
                    event_type = event.type

                    # Log non-audio events (audio deltas are too frequent)
                    if event_type != "response.output_audio.delta":
                        logger.info("realtime_event", event_type=event_type)
                    else:
                        logger.debug("audio_delta_received")

                    # Handle tool calls internally
                    if event_type == "response.function_call_arguments.done":
                        await realtime_session.handle_function_call_event(event)

                    # Forward events to client
                    await client_ws.send_json(
                        {
                            "type": event_type,
                            "event": event.model_dump() if hasattr(event, "model_dump") else {},
                        }
                    )
                    logger.debug("event_forwarded", event_type=event_type)

                except Exception as e:
                    logger.exception("event_forward_error", error=str(e))

        except Exception as e:
            logger.exception("realtime_to_client_error", error=str(e))

    # Run both directions concurrently
    await asyncio.gather(
        client_to_realtime(),
        realtime_to_client(),
        return_exceptions=True,
    )


@router.post("/{public_id}/token")
@limiter.limit("10/minute")
async def get_embed_ephemeral_token(  # noqa: PLR0915
    public_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    origin: str | None = Header(None),
    x_embed_parent_origin: str | None = Header(None),
) -> dict[str, Any]:
    """Get an ephemeral token for OpenAI Realtime API WebRTC connection.

    This public endpoint allows embed widgets to establish direct WebRTC
    connections to OpenAI without proxying through the backend.

    Security:
    - Origin validation against allowed domains
    - Rate limiting (inherited from router)
    - Short-lived tokens (expire in ~60 seconds)
    """
    import httpx

    from app.api.settings import get_user_api_keys
    from app.models.workspace import AgentWorkspace
    from app.services.gpt_realtime import build_instructions_with_language

    log = logger.bind(endpoint="embed_token", public_id=public_id, origin=origin)

    # Get agent
    agent = await get_agent_by_public_id(public_id, db)
    if not agent:
        log.warning("agent_not_found")
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.embed_enabled:
        log.warning("embed_disabled")
        raise HTTPException(status_code=403, detail="Embedding is disabled for this agent")

    if not agent.is_active:
        log.warning("agent_inactive")
        raise HTTPException(status_code=403, detail="Agent is not active")

    site = _require_allowed_site(origin, x_embed_parent_origin, agent, log)

    # Check premium tier
    if agent.pricing_tier not in ("premium", "premium-mini"):
        raise HTTPException(
            status_code=400, detail="WebRTC Realtime only available for Premium tier agents"
        )

    # Get workspace for API key lookup
    workspace_result = await db.execute(
        select(AgentWorkspace).where(AgentWorkspace.agent_id == agent.id).limit(1)
    )
    agent_workspace = workspace_result.scalar_one_or_none()

    if not agent_workspace:
        log.warning("no_workspace_for_agent")
        raise HTTPException(status_code=500, detail="Agent not configured properly")

    # Get OpenAI API key - agent.user_id is now directly the integer user ID
    user_uuid = user_id_to_uuid(agent.user_id)
    user_settings = await get_user_api_keys(
        user_uuid, db, workspace_id=agent_workspace.workspace_id
    )

    # Strictly use workspace API key - no fallback to global key for billing isolation
    if not user_settings or not user_settings.openai_api_key:
        log.warning("workspace_missing_openai_key", workspace_id=str(agent_workspace.workspace_id))
        raise HTTPException(
            status_code=400,
            detail="OpenAI API key not configured for this workspace. Please add it in Settings > Workspace API Keys.",
        )
    api_key = user_settings.openai_api_key
    log.info("using_workspace_openai_key")

    # Determine model based on tier
    realtime_model = (
        "gpt-4o-mini-realtime-preview-2024-12-17"
        if agent.pricing_tier == "premium-mini"
        else "gpt-realtime-2025-08-28"
    )

    # Build session configuration
    agent_voice = agent.voice or "marin"
    session_config: dict[str, Any] = {
        "model": realtime_model,
        "modalities": ["audio", "text"],
        "voice": agent_voice,
    }

    log.info("requesting_ephemeral_token", model=realtime_model)

    # Request ephemeral token from OpenAI
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.openai.com/v1/realtime/sessions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=session_config,
                timeout=30.0,
            )

            if not response.is_success:
                log.error(
                    "openai_token_error",
                    status_code=response.status_code,
                    response_text=response.text,
                )
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"OpenAI API error: {response.text}",
                )

            token_data = response.json()
            log.info("ephemeral_token_created")

            # Get workspace timezone for proper time handling
            ws_query_result = await db.execute(
                select(Workspace).where(Workspace.id == agent_workspace.workspace_id)
            )
            ws_obj = ws_query_result.scalar_one_or_none()
            workspace_timezone = (
                ws_obj.settings.get("timezone", "UTC") if ws_obj and ws_obj.settings else "UTC"
            )

            # Build instructions for the frontend with timezone context
            system_prompt = agent.system_prompt or "You are a helpful voice assistant."
            instructions = build_instructions_with_language(
                system_prompt, agent.language, timezone=workspace_timezone
            )

            # Get tool definitions for this agent (matching realtime.py implementation)
            # Note: For embed widgets, we include tool definitions but execution
            # happens via the /tool-call endpoint
            from app.services.tools.registry import ToolRegistry

            # agent.user_id is now directly the integer user ID
            user_id_int = agent.user_id

            # Get integration credentials for the workspace
            workspace_id = agent_workspace.workspace_id
            integrations = await get_workspace_integrations(
                user_id_to_uuid(agent.user_id), workspace_id, db
            )

            tool_registry = ToolRegistry(
                db=db,
                user_id=user_id_int,
                integrations=integrations,
                workspace_id=workspace_id,
            )
            # Tool definitions go to the browser only when the owner has allowed
            # tools for embedded sessions; /tool-call refuses on the same rule.
            embed_tools_on = tools_allowed(agent.embed_settings)
            tools = (
                tool_registry.get_all_tool_definitions(
                    agent.enabled_tools or [], agent.enabled_tool_ids
                )
                if embed_tools_on
                else []
            )
            embed_session = mint_embed_session(agent_id=str(agent.id), site=site)

            log.info(
                "tools_prepared",
                tool_count=len(tools),
                enabled_tools=agent.enabled_tools,
                enabled_tool_ids=agent.enabled_tool_ids,
                tool_names=[t.get("name") for t in tools],
            )

            return {
                "client_secret": token_data.get("client_secret", {}),
                "agent": {
                    "name": agent.name,
                    "voice": agent_voice,
                    "instructions": instructions,
                    "language": agent.language,
                    "initial_greeting": agent.initial_greeting,
                },
                "model": realtime_model,
                "tools": tools,
                # Every later call from this widget (/tool-call, /transcript)
                # must send this back in the X-Embed-Session header.
                "session_token": embed_session.token,
                "session_expires_in": embed_session.expires_in,
            }

    except httpx.RequestError as e:
        log.exception("openai_token_request_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to connect to OpenAI: {e!s}") from e


class ToolCallRequest(BaseModel):
    """Request model for tool execution."""

    tool_name: str
    arguments: dict[str, Any]


class SaveTranscriptRequest(BaseModel):
    """Request model for saving call transcript."""

    session_id: str
    transcript: str
    duration_seconds: int = 0


@router.post("/{public_id}/tool-call")
@limiter.limit("120/minute")
@limiter.limit("30/minute", key_func=_session_key)
async def execute_embed_tool_call(
    public_id: str,
    tool_request: ToolCallRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    origin: str | None = Header(None),
    x_embed_parent_origin: str | None = Header(None),
    x_embed_session: str | None = Header(None),
) -> dict[str, Any]:
    """Execute a tool call for an embed widget.

    This endpoint allows the frontend to proxy tool calls from OpenAI
    Realtime API to the backend for execution with proper credentials.

    Security:
    - Origin validation against allowed domains
    - Only tools enabled for the agent are executed
    """

    log = logger.bind(
        endpoint="embed_tool_call",
        public_id=public_id,
        tool_name=tool_request.tool_name,
        origin=origin,
    )

    # Get agent
    agent = await get_agent_by_public_id(public_id, db)
    if not agent:
        log.warning("agent_not_found")
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.embed_enabled or not agent.is_active:
        log.warning("agent_not_available")
        raise HTTPException(status_code=403, detail="Agent not available")

    _require_session(x_embed_session, origin, x_embed_parent_origin, agent, log)

    if not tools_allowed(agent.embed_settings):
        log.warning("embed_tools_not_allowed")
        raise HTTPException(
            status_code=403, detail="Tools are not enabled for embedded sessions of this agent"
        )

    # agent.user_id is now directly the integer user ID
    user_id_int = agent.user_id

    # Get workspace for this agent (needed for proper CRM scoping)
    from app.models.workspace import AgentWorkspace

    workspace_result = await db.execute(
        select(AgentWorkspace).where(AgentWorkspace.agent_id == agent.id).limit(1)
    )
    agent_workspace = workspace_result.scalar_one_or_none()
    workspace_id = agent_workspace.workspace_id if agent_workspace else None

    # Get integration credentials for the workspace
    integrations: dict[str, dict[str, Any]] = {}
    if workspace_id:
        integrations = await get_workspace_integrations(
            user_id_to_uuid(agent.user_id), workspace_id, db
        )

    # Create tool registry with workspace context
    from app.services.tools.registry import ToolRegistry

    tool_registry = ToolRegistry(
        db=db,
        user_id=user_id_int,
        integrations=integrations,
        workspace_id=workspace_id,
    )

    # Get the enabled tools for this agent (same method as token endpoint)
    enabled_tool_defs = tool_registry.get_all_tool_definitions(
        agent.enabled_tools or [], agent.enabled_tool_ids
    )
    enabled_tool_names = {
        t.get("name") or t.get("function", {}).get("name") for t in enabled_tool_defs
    }

    # Validate the requested tool is in the enabled tools list
    if tool_request.tool_name not in enabled_tool_names:
        log.warning(
            "tool_not_enabled",
            requested=tool_request.tool_name,
            enabled=list(enabled_tool_names),
        )
        return {
            "success": False,
            "error": f"Tool '{tool_request.tool_name}' is not enabled for this agent",
        }

    try:
        result = await tool_registry.execute_tool(
            tool_request.tool_name,
            tool_request.arguments,
        )
        log.info("tool_executed", success=result.get("success"))
        return result
    except Exception as e:
        log.exception("tool_execution_error", error=str(e))
        return {"success": False, "error": str(e)}
    finally:
        await tool_registry.close()


@router.post("/{public_id}/transcript")
@limiter.limit("20/minute")
@limiter.limit("3/minute", key_func=_session_key)
async def save_embed_transcript(
    public_id: str,
    transcript_request: SaveTranscriptRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    origin: str | None = Header(None),
    x_embed_parent_origin: str | None = Header(None),
    x_embed_session: str | None = Header(None),
) -> dict[str, Any]:
    """Save a call transcript from an embed widget session.

    This endpoint creates a CallRecord with the transcript for widget calls.
    Widget calls use 'widget' as the provider since they're not telephony calls.

    Security:
    - Origin validation against allowed domains
    - Only saves for active, embed-enabled agents
    """
    from app.models.call_record import CallDirection, CallRecord, CallStatus
    from app.models.workspace import AgentWorkspace

    log = logger.bind(
        endpoint="embed_transcript",
        public_id=public_id,
        session_id=transcript_request.session_id,
        origin=origin,
    )

    # Get agent
    agent = await get_agent_by_public_id(public_id, db)
    if not agent:
        log.warning("agent_not_found")
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.embed_enabled or not agent.is_active:
        log.warning("agent_not_available")
        raise HTTPException(status_code=403, detail="Agent not available")

    embed_session = _require_session(x_embed_session, origin, x_embed_parent_origin, agent, log)

    # Check if transcripts are enabled for this agent
    if not agent.enable_transcript:
        log.info("transcripts_disabled_for_agent")
        return {"success": True, "message": "Transcripts not enabled for this agent"}

    # Skip if transcript is empty
    if not transcript_request.transcript.strip():
        log.info("empty_transcript_skipped")
        return {"success": True, "message": "Empty transcript skipped"}

    # Get workspace for this agent
    workspace_result = await db.execute(
        select(AgentWorkspace).where(AgentWorkspace.agent_id == agent.id).limit(1)
    )
    agent_workspace = workspace_result.scalar_one_or_none()
    workspace_id = agent_workspace.workspace_id if agent_workspace else None

    # Create call record for widget session
    call_record = CallRecord(
        # call_records keys its owner by UUID while Agent carries the integer
        # user id; the integer raises on insert against Postgres.
        user_id=user_id_to_uuid(agent.user_id),
        workspace_id=workspace_id,
        provider="widget",
        # The session the server minted, not an identifier the browser chose.
        provider_call_id=f"embed-{embed_session.session_id}",
        agent_id=agent.id,
        direction=CallDirection.INBOUND.value,
        status=CallStatus.COMPLETED.value,
        from_number="widget",
        to_number="widget",
        duration_seconds=transcript_request.duration_seconds,
        transcript=transcript_request.transcript,
        started_at=datetime.now(UTC) - timedelta(seconds=transcript_request.duration_seconds),
        ended_at=datetime.now(UTC),
    )
    db.add(call_record)
    await db.commit()

    log.info(
        "transcript_saved",
        record_id=str(call_record.id),
        transcript_length=len(transcript_request.transcript),
        duration_seconds=transcript_request.duration_seconds,
    )

    return {"success": True, "call_id": str(call_record.id)}
