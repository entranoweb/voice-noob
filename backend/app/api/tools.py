"""API endpoints for tool execution."""

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.integrations import get_workspace_integrations
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.ownership import get_owned_agent, owned_agent_workspace_id
from app.db.session import get_db
from app.services.tools.registry import ToolRegistry

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])
logger = structlog.get_logger()


class ToolExecuteRequest(BaseModel):
    """Request body for tool execution."""

    tool_name: str
    arguments: dict[str, Any]
    agent_id: str


@router.post("/execute")
async def execute_tool(
    request: ToolExecuteRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Execute a tool and return the result.

    This endpoint is called by the frontend when the GPT Realtime model
    makes a function call. The tool is executed on the backend and the
    result is returned to be sent back to the model.

    Args:
        request: Tool execution request
        current_user: Authenticated user
        db: Database session

    Returns:
        Tool execution result
    """
    user_id = current_user.id
    tool_logger = logger.bind(
        endpoint="execute_tool",
        tool_name=request.tool_name,
        agent_id=request.agent_id,
        user_id=user_id,
    )

    tool_logger.info("tool_execution_requested", arguments=request.arguments)

    # The agent id comes from the client. It selects whose contacts, bookings
    # and integration credentials the tool acts on, so it has to be the
    # caller's own agent: get_owned_agent answers 404 for anyone else's.
    agent = await get_owned_agent(db, request.agent_id, current_user)
    workspace_id: uuid.UUID | None = await owned_agent_workspace_id(db, agent, current_user)

    try:
        # Get integration credentials for the workspace
        integrations: dict[str, dict[str, Any]] = {}
        if workspace_id:
            user_uuid = user_id_to_uuid(user_id)
            integrations = await get_workspace_integrations(user_uuid, workspace_id, db)

        # Create tool registry and execute tool
        tool_registry = ToolRegistry(
            db, user_id, integrations=integrations, workspace_id=workspace_id
        )

        # Only a tool the agent's owner switched on may run. Without this the
        # route is a general-purpose executor for every tool in the registry.
        allowed = tool_registry.enabled_tool_names(
            agent.enabled_tools or [], agent.enabled_tool_ids
        )
        if request.tool_name not in allowed:
            tool_logger.warning("tool_not_enabled_for_agent")
            raise HTTPException(status_code=403, detail="Tool is not enabled for this agent")

        result = await tool_registry.execute_tool(request.tool_name, request.arguments)

        tool_logger.info("tool_execution_completed", success=result.get("success", False))

        return result

    except HTTPException:
        raise
    except Exception as e:
        tool_logger.exception("tool_execution_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Tool execution failed") from e
