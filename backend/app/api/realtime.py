"""WebRTC and WebSocket API for GPT Realtime voice calls."""

import asyncio
import contextlib
import json
import uuid
from http import HTTPStatus
from typing import Any

import httpx
import jwt
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.integrations import get_workspace_integrations
from app.api.settings import get_user_api_keys
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.config import settings
from app.core.limiter import limiter
from app.core.ownership import get_owned_agent, owned_agent_workspace_id, parse_uuid
from app.db.session import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services.gpt_realtime import GPTRealtimeSession, build_instructions_with_language
from app.services.realtime_models import (
    ClientSecretError,
    input_transcription,
    parse_client_secret,
    post_client_secret,
    realtime_model_for_tier,
)
from app.services.tools.registry import ToolRegistry

router = APIRouter(prefix="/ws", tags=["realtime"])
webrtc_router = APIRouter(prefix="/api/v1/realtime", tags=["realtime-webrtc"])
logger = structlog.get_logger()

# Per client address, like the rest of the API. Each token or session starts a
# billed model session on the workspace's key, and a dashboard test needs one
# or two a minute at most.
REALTIME_TOKEN_LIMIT = "20/minute"
REALTIME_SESSION_LIMIT = "20/minute"


async def get_openai_api_key_for_workspace(
    user_uuid: uuid.UUID,
    workspace_uuid: uuid.UUID,
    db: AsyncSession,
    log: structlog.BoundLogger,
) -> str:
    """The OpenAI key stored for exactly this workspace - no fallback of any kind.

    Neither the user-level key nor the platform ``OPENAI_API_KEY`` stands in for
    a missing workspace key: that would bill the platform (or another workspace)
    for a tenant who never added one. The telephony bridge behaves the same way.

    Raises:
        HTTPException: 400 if no API key is stored for the workspace
    """
    user_settings = await get_user_api_keys(user_uuid, db, workspace_id=workspace_uuid)
    if user_settings and user_settings.openai_api_key:
        log.info("using_workspace_openai_key", workspace_id=str(workspace_uuid))
        return user_settings.openai_api_key

    log.warning("workspace_missing_openai_key", workspace_id=str(workspace_uuid))
    raise HTTPException(
        status_code=400,
        detail="OpenAI API key not configured for this workspace. Please add it in Settings > Workspace API Keys.",
    )


async def resolve_agent_workspace(
    db: AsyncSession,
    agent: Agent,
    current_user: User,
    workspace_id: str | None,
) -> uuid.UUID:
    """The workspace an owned agent runs in, which is where its key comes from.

    The client may name the workspace, but only to confirm it: naming any
    workspace other than the agent's own is a 404, so a request cannot pick
    which workspace's key is used. An agent in no workspace has no key to use.
    """
    requested = parse_uuid(workspace_id, "Workspace") if workspace_id else None
    agent_workspace = await owned_agent_workspace_id(db, agent, current_user)
    if requested is not None and requested != agent_workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if agent_workspace is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "This agent is not in a workspace, so there is no OpenAI key to use. "
                "Add it to a workspace and set the key in Settings > Workspace API Keys."
            ),
        )
    return agent_workspace


def get_realtime_model_for_tier(pricing_tier: str) -> str:
    """Get the appropriate Realtime model based on pricing tier.

    Args:
        pricing_tier: Agent pricing tier

    Returns:
        OpenAI Realtime model name
    """
    return realtime_model_for_tier(pricing_tier)


def dashboard_token_session_config(model: str, voice: str) -> dict[str, Any]:
    """The GA session a dashboard test key is minted for.

    Minimal on purpose: the page sets instructions, tools and turn detection
    after it connects, because the test page lets the owner edit them live.
    """
    audio_input: dict[str, Any] = {}
    if (transcription := input_transcription()) is not None:
        audio_input["transcription"] = transcription
    session: dict[str, Any] = {
        "type": "realtime",
        "model": model,
        "audio": {"output": {"voice": voice}},
    }
    if audio_input:
        session["audio"]["input"] = audio_input
    return session


# Close codes for a refused dashboard socket: who you are, then what you may reach.
REALTIME_SOCKET_UNAUTHENTICATED = 4401
REALTIME_SOCKET_FORBIDDEN = 4403


async def _user_from_socket_token(token: str | None, db: AsyncSession) -> User | None:
    """The signed-in user a socket's access token belongs to, or None.

    The same check `get_current_user` makes for HTTP requests: a token this
    application signed, not expired, naming a user that still exists and is
    active.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = int(payload.get("sub") or 0)
    except (jwt.PyJWTError, TypeError, ValueError):
        return None
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


@router.websocket("/realtime/{agent_id}")
async def realtime_websocket(  # noqa: PLR0911, PLR0915
    websocket: WebSocket,
    agent_id: str,
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """WebSocket endpoint for GPT Realtime voice calls.

    This endpoint:
    1. Accepts WebSocket connection from client (phone/browser)
    2. Loads agent configuration and enabled integrations
    3. Initializes GPT Realtime session with internal tools
    4. Bridges audio between client and GPT Realtime
    5. Routes tool calls to internal tool handlers

    Args:
        websocket: WebSocket connection
        agent_id: Agent UUID
        workspace_id: Workspace UUID (required for API key isolation)
        db: Database session
    """
    session_id = str(uuid.uuid4())
    client_logger = logger.bind(
        endpoint="realtime_websocket",
        agent_id=agent_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )

    # This socket is for a signed-in dashboard user talking to their own agent.
    # It used to accept anyone: an agent id and a workspace id were enough to
    # open a model session on the owner's key with the agent's tools. A browser
    # cannot set an Authorization header on a websocket, so the user's access
    # token travels as the `token` query parameter and is checked, along with
    # ownership of the agent, before the connection is accepted.
    user = await _user_from_socket_token(websocket.query_params.get("token"), db)
    if user is None:
        client_logger.warning("realtime_socket_rejected", reason="unauthenticated")
        await websocket.close(code=REALTIME_SOCKET_UNAUTHENTICATED)
        return

    try:
        agent_uuid = uuid.UUID(agent_id)
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError:
        client_logger.warning("invalid_uuid_format")
        await websocket.close(code=4000)
        return

    owned = await db.execute(
        select(Agent.id).where(Agent.id == agent_uuid, Agent.user_id == user.id)
    )
    if owned.scalar_one_or_none() is None:
        # The same answer whether the agent does not exist or belongs to someone
        # else, so the socket cannot be used to test which agent ids are real.
        client_logger.warning("realtime_socket_rejected", reason="not_owner", user_id=user.id)
        await websocket.close(code=REALTIME_SOCKET_FORBIDDEN)
        return

    await websocket.accept()
    client_logger.info("websocket_connected", user_id=user.id)

    try:
        # Load agent configuration with workspace verification
        from app.models.workspace import AgentWorkspace

        result = await db.execute(select(Agent).where(Agent.id == agent_uuid))
        agent = result.scalar_one_or_none()

        if not agent:
            await websocket.send_json(
                {
                    "type": "error",
                    "error": f"Agent {agent_id} not found",
                }
            )
            await websocket.close()
            return

        if not agent.is_active:
            await websocket.send_json(
                {
                    "type": "error",
                    "error": "Agent is not active",
                }
            )
            await websocket.close()
            return

        # Check if Premium tier (GPT Realtime only for Premium/Premium-Mini)
        if agent.pricing_tier not in ("premium", "premium-mini"):
            await websocket.send_json(
                {
                    "type": "error",
                    "error": "GPT Realtime only available for Premium tier agents",
                }
            )
            await websocket.close()
            return

        # Verify agent is associated with the specified workspace (authorization check)
        workspace_check = await db.execute(
            select(AgentWorkspace).where(
                AgentWorkspace.agent_id == agent_uuid,
                AgentWorkspace.workspace_id == workspace_uuid,
            )
        )
        if not workspace_check.scalar_one_or_none():
            client_logger.warning(
                "unauthorized_workspace_access",
                agent_id=agent_id,
                workspace_id=workspace_id,
            )
            await websocket.send_json(
                {"type": "error", "error": "Agent not authorized for this workspace"}
            )
            await websocket.close(code=4003)
            return

        client_logger.info(
            "agent_loaded",
            agent_name=agent.name,
            tier=agent.pricing_tier,
            tools_count=len(agent.enabled_tools),
        )

        # agent.user_id is now directly the integer user ID
        user_id_int = agent.user_id

        # Build agent config for GPT Realtime
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "enabled_tool_ids": agent.enabled_tool_ids,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            # The session picks its model from the tier (premium-mini is not premium).
            "pricing_tier": agent.pricing_tier,
        }

        # Initialize GPT Realtime session with internal tools
        async with GPTRealtimeSession(
            db=db,
            user_id=user_id_int,
            agent_config=agent_config,
            session_id=session_id,
            workspace_id=uuid.UUID(workspace_id),
        ) as realtime_session:
            # Send ready signal to client
            await websocket.send_json(
                {
                    "type": "session.ready",
                    "session_id": session_id,
                    "agent": {
                        "id": str(agent.id),
                        "name": agent.name,
                        "tier": agent.pricing_tier,
                    },
                }
            )

            # Start bidirectional streaming
            await _bridge_audio_streams(
                client_ws=websocket,
                realtime_session=realtime_session,
                logger=client_logger,
            )

    except WebSocketDisconnect:
        client_logger.info("websocket_disconnected")
    except Exception as e:
        client_logger.exception("websocket_error", error=str(e))
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {
                    "type": "error",
                    "error": str(e),
                }
            )
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
        client_logger.info("websocket_closed")


async def _bridge_audio_streams(
    client_ws: WebSocket,
    realtime_session: GPTRealtimeSession,
    logger: Any,
) -> None:
    """Bridge audio streams between client and GPT Realtime.

    Args:
        client_ws: Client WebSocket connection
        realtime_session: GPT Realtime session
        logger: Structured logger
    """

    async def client_to_realtime() -> None:
        """Forward messages from client to GPT Realtime."""
        try:
            while True:
                # Receive from client
                logger.debug("waiting_for_client_message")
                message = await client_ws.receive()
                logger.debug("client_message_received", message_type=message.get("type"))

                if message["type"] == "websocket.disconnect":
                    logger.info("client_initiated_disconnect")
                    break

                # Forward to Realtime API
                if message["type"] == "websocket.receive":
                    if "bytes" in message:
                        # Audio data
                        audio_size = len(message["bytes"])
                        logger.debug("client_audio_received", size_bytes=audio_size)
                        await realtime_session.send_audio(message["bytes"])
                    elif "text" in message:
                        # JSON event - with error handling for malformed JSON
                        try:
                            data = json.loads(message["text"])
                            logger.info("client_event", event_type=data.get("type"), data=data)
                        except json.JSONDecodeError as e:
                            logger.warning("invalid_json_from_client", error=str(e))
                            continue  # Skip malformed message

        except WebSocketDisconnect:
            logger.info("client_disconnected_exception")
        except Exception as e:
            logger.exception("client_to_realtime_error", error=str(e), error_type=type(e).__name__)

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

                    logger.info("realtime_event", event_type=event_type)

                    # Handle tool calls internally
                    if event_type == "response.function_call_arguments.done":
                        logger.info(
                            "handling_function_call", call_id=event.call_id, name=event.name
                        )
                        await realtime_session.handle_function_call_event(event)

                    # Forward events to client as JSON
                    await client_ws.send_json(
                        {
                            "type": event_type,
                            "event": event.model_dump() if hasattr(event, "model_dump") else {},
                        }
                    )
                    logger.debug("event_forwarded_to_client", event_type=event_type)

                except Exception as e:
                    logger.exception(
                        "event_forward_error", error=str(e), error_type=type(e).__name__
                    )

        except Exception as e:
            logger.exception("realtime_to_client_error", error=str(e), error_type=type(e).__name__)

    # Run both directions concurrently and check for errors
    results = await asyncio.gather(
        client_to_realtime(),
        realtime_to_client(),
        return_exceptions=True,
    )

    # Check and log any exceptions from the concurrent tasks
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            task_name = "client_to_realtime" if i == 0 else "realtime_to_client"
            logger.error(
                "bridge_task_failed",
                task=task_name,
                error=str(result),
                error_type=type(result).__name__,
            )


# =============================================================================
# WebRTC Endpoints for GPT Realtime
# =============================================================================


@webrtc_router.post("/session/{agent_id}")
@limiter.limit(REALTIME_SESSION_LIMIT)
async def create_webrtc_session(
    request: Request,
    agent_id: str,
    workspace_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Create a WebRTC session for GPT Realtime API.

    This endpoint implements the unified interface approach:
    1. Receives SDP offer from client
    2. Loads agent configuration and builds session config with tools
    3. Forwards to OpenAI Realtime API
    4. Returns SDP answer to client

    Args:
        agent_id: Agent UUID
        workspace_id: Workspace UUID (required for API key isolation)
        request: HTTP request containing SDP offer
        current_user: Authenticated user
        db: Database session

    Returns:
        SDP answer from OpenAI
    """
    user_id = current_user.id
    user_uuid = user_id_to_uuid(user_id)
    session_logger = logger.bind(
        endpoint="webrtc_session",
        agent_id=agent_id,
        user_id=user_id,
    )

    session_logger.info("webrtc_session_requested")

    # Get SDP offer from request body
    sdp_offer = await request.body()
    if not sdp_offer:
        raise HTTPException(status_code=400, detail="SDP offer required in request body")

    # Load the agent, which must be the caller's own: this route runs the
    # agent's prompt on its owner's key, and returns its configuration.
    agent = await get_owned_agent(db, agent_id, current_user)

    if not agent.is_active:
        raise HTTPException(status_code=400, detail="Agent is not active")

    if agent.pricing_tier not in ("premium", "premium-mini"):
        raise HTTPException(
            status_code=400, detail="WebRTC Realtime only available for Premium tier agents"
        )

    # Determine which model to use based on tier
    realtime_model = get_realtime_model_for_tier(agent.pricing_tier)

    session_logger.info(
        "agent_loaded",
        agent_name=agent.name,
        tier=agent.pricing_tier,
        model=realtime_model,
        tools_count=len(agent.enabled_tools),
    )

    # The key is the agent's own workspace's (user_uuid for UserSettings lookup)
    workspace_uuid = await resolve_agent_workspace(db, agent, current_user, workspace_id)
    api_key = await get_openai_api_key_for_workspace(user_uuid, workspace_uuid, db, session_logger)

    # Get integration credentials for the workspace
    integrations = await get_workspace_integrations(user_uuid, workspace_uuid, db)

    # Build tool definitions (user_id int for Contact queries, workspace_uuid for scoping)
    tool_registry = ToolRegistry(
        db, user_id, integrations=integrations, workspace_id=workspace_uuid
    )
    tools = tool_registry.get_all_tool_definitions(agent.enabled_tools, agent.enabled_tool_ids)

    # Build instructions with language directive
    system_prompt = agent.system_prompt or "You are a helpful voice assistant."
    instructions = build_instructions_with_language(system_prompt, agent.language)

    # Build session configuration for OpenAI Realtime
    # Use agent's configured voice (default to marin for natural conversational tone)
    agent_voice = agent.voice or "marin"
    # `/v1/realtime/calls` takes a GA session object, which puts the audio
    # settings under `audio.input` / `audio.output` and has no `temperature`.
    # The formats are left unset on purpose: this is a WebRTC call and the codec
    # is negotiated in the SDP, unlike the telephony bridge where the carrier
    # fixes it at mu-law (see `_assert_ga_session` in services/gpt_realtime.py).
    transcription = input_transcription()
    session_config: dict[str, Any] = {
        "type": "realtime",
        "model": realtime_model,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                **({"transcription": transcription} if transcription else {}),
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": agent.turn_detection_threshold or 0.5,
                    "prefix_padding_ms": agent.turn_detection_prefix_padding_ms or 200,
                    "silence_duration_ms": agent.turn_detection_silence_duration_ms or 200,
                },
            },
            "output": {
                "voice": agent_voice,
                "speed": 1.1,  # 1.0 = normal, range 0.25-1.5
            },
        },
    }

    # Add tools if any are enabled
    if tools:
        session_config["tools"] = tools
        session_config["tool_choice"] = "auto"

    session_logger.info(
        "creating_openai_session",
        model=session_config["model"],
        tool_count=len(tools),
    )

    # Create multipart form data for OpenAI API
    try:
        async with httpx.AsyncClient() as client:
            # Prepare multipart form - properly typed for httpx
            files: list[tuple[str, tuple[str, bytes | str, str]]] = [
                ("sdp", ("offer.sdp", sdp_offer, "application/sdp")),
                ("session", ("session.json", json.dumps(session_config), "application/json")),
            ]

            response = await client.post(
                "https://api.openai.com/v1/realtime/calls",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                timeout=30.0,
            )

            if response.status_code != HTTPStatus.OK:
                session_logger.error(
                    "openai_api_error",
                    status_code=response.status_code,
                    response_text=response.text,
                )
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"OpenAI API error: {response.text}",
                )

            sdp_answer = response.text
            session_logger.info("webrtc_session_created")

            return Response(content=sdp_answer, media_type="application/sdp")

    except httpx.RequestError as e:
        session_logger.exception("openai_request_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to connect to OpenAI: {e!s}") from e


@webrtc_router.get("/token/{agent_id}")
@limiter.limit(REALTIME_TOKEN_LIMIT)
async def get_ephemeral_token(
    request: Request,
    agent_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Get an ephemeral token for OpenAI Realtime API WebRTC connection.

    This endpoint is used by the OpenAI Agents SDK to establish WebRTC connections.
    It returns a short-lived ephemeral API key that can be used client-side.

    Args:
        agent_id: Agent UUID
        current_user: Authenticated user
        db: Database session
        workspace_id: Optional; if given it must be the agent's own workspace (404 otherwise).
            The OpenAI key is that workspace's, with no user-level or platform fallback.

    Returns:
        Ephemeral token response with client_secret value and session config
    """
    user_id = current_user.id
    user_uuid = user_id_to_uuid(user_id)
    token_logger = logger.bind(
        endpoint="ephemeral_token",
        agent_id=agent_id,
        user_id=user_id,
    )

    token_logger.info("ephemeral_token_requested")

    # Load the agent, which must be the caller's own: this route runs the
    # agent's prompt on its owner's key, and returns its configuration.
    agent = await get_owned_agent(db, agent_id, current_user)

    if not agent.is_active:
        raise HTTPException(status_code=400, detail="Agent is not active")

    if agent.pricing_tier not in ("premium", "premium-mini"):
        raise HTTPException(
            status_code=400, detail="WebRTC Realtime only available for Premium tier agents"
        )

    # Determine which model to use based on tier
    realtime_model = get_realtime_model_for_tier(agent.pricing_tier)

    # The key is the agent's own workspace's (user_uuid for UserSettings lookup)
    workspace_uuid = await resolve_agent_workspace(db, agent, current_user, workspace_id)
    api_key = await get_openai_api_key_for_workspace(user_uuid, workspace_uuid, db, token_logger)

    # Minimal GA session for the key; the page configures the rest over the data channel.
    agent_voice = agent.voice or "shimmer"
    session_config = dashboard_token_session_config(realtime_model, agent_voice)

    token_logger.info(
        "requesting_ephemeral_token",
        model=session_config["model"],
    )

    # GA endpoint: POST /v1/realtime/client_secrets with the session wrapped in
    # {"session": ...}; the key comes back as `value`.
    try:
        response = await post_client_secret(api_key, session_config)
        if not response.is_success:
            token_logger.error(
                "openai_token_error",
                status_code=response.status_code,
                response_text=response.text,
            )
            raise HTTPException(
                status_code=response.status_code,
                detail=f"OpenAI API error: {response.text}",
            )

        try:
            key = parse_client_secret(response)
        except ClientSecretError as e:
            token_logger.exception("openai_token_unusable", error=str(e))
            raise HTTPException(
                status_code=502, detail="The voice provider returned no session key"
            ) from e
        token_logger.info("ephemeral_token_created")

        # Get integration credentials for the workspace
        integrations = await get_workspace_integrations(user_uuid, workspace_uuid, db)

        # Build tool definitions for the agent
        tool_registry = ToolRegistry(
            db, user_id, integrations=integrations, workspace_id=workspace_uuid
        )
        tools = tool_registry.get_all_tool_definitions(agent.enabled_tools, agent.enabled_tool_ids)

        token_logger.info(
            "tools_prepared",
            tool_count=len(tools),
            enabled_tools=agent.enabled_tools,
            enabled_tool_ids=agent.enabled_tool_ids,
            tool_names=[t.get("name") for t in tools],
        )

        # Build instructions with language for the frontend to use
        system_prompt = agent.system_prompt or "You are a helpful voice assistant."
        instructions_with_language = build_instructions_with_language(system_prompt, agent.language)

        # Return token data with agent info and tools
        return {
            "value": key["value"],
            "expires_at": key["expires_at"],
            "agent": {
                "id": str(agent.id),
                "name": agent.name,
                "tier": agent.pricing_tier,
                "system_prompt": agent.system_prompt,
                "language": agent.language,
                "voice": agent_voice,
                "instructions": instructions_with_language,
                "enabled_tools": agent.enabled_tools,
                "initial_greeting": agent.initial_greeting,
            },
            "session_config": session_config,
            "tools": tools,
        }

    except httpx.RequestError as e:
        token_logger.exception("openai_token_request_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to connect to OpenAI: {e!s}") from e


# =============================================================================
# Transcript Saving Endpoint
# =============================================================================


class SaveTranscriptRequest(BaseModel):
    """Request body for saving a transcript."""

    session_id: str
    transcript: str
    duration_seconds: int = 0


@webrtc_router.post("/transcript/{agent_id}")
async def save_transcript(
    agent_id: str,
    request: SaveTranscriptRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Save a transcript from a Test Agent or WebRTC session.

    This endpoint creates a CallRecord with provider="test" for test sessions.

    Args:
        agent_id: Agent UUID
        request: Transcript data
        current_user: Authenticated user
        db: Database session
        workspace_id: Optional workspace UUID

    Returns:
        Success response with call record ID
    """
    from datetime import UTC, datetime, timedelta

    from app.models.call_record import CallRecord
    from app.models.workspace import AgentWorkspace

    user_id = current_user.id
    transcript_logger = logger.bind(
        endpoint="save_transcript",
        agent_id=agent_id,
        user_id=user_id,
        session_id=request.session_id,
    )

    transcript_logger.info("saving_transcript", length=len(request.transcript))

    # The agent must be the caller's own. (This compared the owner's integer id
    # with a UUID and so refused every request, including the owner's.)
    agent = await get_owned_agent(db, agent_id, current_user)
    user_uuid = user_id_to_uuid(user_id)

    # Skip if transcript is empty
    if not request.transcript.strip():
        transcript_logger.debug("empty_transcript_skipped")
        return {"success": True, "message": "Empty transcript skipped"}

    # Get workspace for this agent (like embed does)
    workspace_result = await db.execute(
        select(AgentWorkspace).where(AgentWorkspace.agent_id == agent.id).limit(1)
    )
    agent_workspace = workspace_result.scalar_one_or_none()
    agent_workspace_id = agent_workspace.workspace_id if agent_workspace else None

    # Create call record with proper timestamps
    ended_at = datetime.now(UTC)
    started_at = ended_at - timedelta(seconds=request.duration_seconds)

    call_record = CallRecord(
        user_id=user_uuid,
        workspace_id=agent_workspace_id,
        agent_id=uuid.UUID(agent_id),
        provider="test",
        provider_call_id=request.session_id,
        direction="outbound",
        from_number="test",
        to_number="test",
        status="completed",
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=request.duration_seconds,
        transcript=request.transcript,
    )

    db.add(call_record)
    await db.commit()
    await db.refresh(call_record)

    transcript_logger.info(
        "transcript_saved",
        record_id=str(call_record.id),
        duration=request.duration_seconds,
    )

    return {
        "success": True,
        "call_id": str(call_record.id),
    }
