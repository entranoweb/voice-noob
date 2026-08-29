"""Bind an agent's real tools into a simulated test run.

The point of this module is that a simulated call executes the *same* tools the
agent would execute on a live call, against the same database. Nothing is
mocked. That is what makes ``task_completion`` mean something: after the run we
snapshot the CRM and compare it to what the scenario said should be there, so a
scenario fails when the booking did not happen — not when a model decides the
transcript sounded unhelpful.

Two deliberate restrictions:

* Only tools that act on our own data are bound. Third-party integrations
  (GoHighLevel, Calendly, Shopify, SMS) are excluded by passing no credentials,
  so a test run can never send a real customer an SMS or write into someone's
  live calendar.
* Arguments are validated against the tool's own schema before execution. A
  malformed call is recorded as ``INVALID_ARGS`` and never executed, which keeps
  a model failure distinguishable from a downstream one.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from app.monitoring.call_trace import ToolOutcome
from app.services.tools.registry import ToolRegistry

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent

logger = structlog.get_logger()

# Integrations whose tools reach outside our database. Binding these in a test
# would make a test run visible to a real customer, so a simulation never gets
# them regardless of what the agent has enabled.
EXTERNAL_INTEGRATIONS = frozenset(
    {
        "gohighlevel",
        "calendly",
        "shopify",
        "twilio-sms",
        "telnyx-sms",
    },
)

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _unwrap(definition: dict[str, Any]) -> dict[str, Any]:
    """Return the body of a tool definition in either OpenAI shape.

    The registry emits the flat Realtime shape (``{"type", "name", ...}``) while
    some integrations use the nested chat-completions shape
    (``{"type": "function", "function": {...}}``). Accept both rather than
    forcing every integration to agree first.
    """
    nested = definition.get("function")
    return nested if isinstance(nested, dict) else definition


def to_anthropic_tools(definitions: list[Any]) -> list[dict[str, Any]]:
    """Convert registry tool definitions to the Anthropic tool schema.

    Duplicate names are dropped, first one wins: the registry returns the CRM
    definitions once for ``crm`` and again for ``bookings``, and the API rejects
    a request that names the same tool twice.
    """
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()

    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        body = _unwrap(definition)
        name = body.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        schema = body.get("parameters") or body.get("input_schema") or _EMPTY_SCHEMA
        seen.add(name)
        tools.append(
            {
                "name": name,
                "description": body.get("description") or "",
                "input_schema": schema if isinstance(schema, dict) else _EMPTY_SCHEMA,
            },
        )
    return tools


def _missing_required(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Names the schema requires that the call did not supply."""
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [
        name
        for name in required
        if isinstance(name, str) and (name not in arguments or arguments[name] in (None, ""))
    ]


class BoundTools:
    """An agent's executable tools for the duration of one simulated run."""

    def __init__(self, registry: ToolRegistry, tools: list[dict[str, Any]]) -> None:
        self._registry = registry
        self._tools = tools
        self._schemas = {tool["name"]: tool["input_schema"] for tool in tools}

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Tool definitions in Anthropic schema, ready to pass to the API."""
        return self._tools

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._schemas)

    def __bool__(self) -> bool:
        return bool(self._tools)

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute one tool for real and return a record of the invocation.

        The record is the shape ``metrics.context.tool_calls_from_records``
        reads, so what the metrics score is exactly what happened.
        """
        started = time.monotonic()

        schema = self._schemas.get(name)
        if schema is None:
            return self._record(
                name,
                arguments,
                started,
                outcome=ToolOutcome.ERROR,
                error=f"Unknown tool: {name}",
                result={"success": False, "error": f"Unknown tool: {name}"},
            )

        missing = _missing_required(schema, arguments)
        if missing:
            # Not executed on purpose: a call missing required arguments is a
            # model failure, and running it anyway would turn it into a
            # downstream error that reads like an outage.
            message = f"Missing required argument(s): {', '.join(missing)}"
            return self._record(
                name,
                arguments,
                started,
                outcome=ToolOutcome.INVALID_ARGS,
                error=message,
                result={"success": False, "error": message},
            )

        try:
            result = await self._registry.execute_tool(name, arguments)
        except Exception as exc:  # a failing tool is data, not a crash
            logger.warning("qa_tool_execution_failed", tool=name, error=str(exc))
            return self._record(
                name,
                arguments,
                started,
                outcome=ToolOutcome.ERROR,
                error=str(exc),
                result={"success": False, "error": str(exc)},
            )

        error = result.get("error") if isinstance(result, dict) else None
        failed = isinstance(result, dict) and result.get("success") is False
        return self._record(
            name,
            arguments,
            started,
            outcome=ToolOutcome.ERROR if (failed or error) else ToolOutcome.OK,
            error=str(error) if error else None,
            result=result,
        )

    @staticmethod
    def _record(
        name: str,
        arguments: dict[str, Any],
        started: float,
        *,
        outcome: ToolOutcome,
        error: str | None,
        result: Any,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "arguments": arguments,
            "outcome": str(outcome),
            "error": error,
            "duration_ms": (time.monotonic() - started) * 1000.0,
            "result": result,
        }

    async def close(self) -> None:
        await self._registry.close()


async def bind_agent_tools(
    db: AsyncSession,
    agent: Agent,
    user_id: int,
    workspace_id: uuid.UUID | None = None,
) -> BoundTools:
    """Build the executable tool set for one agent under test.

    Passes no integration credentials, so ``get_all_tool_definitions`` skips
    every external integration even if the agent has it enabled.
    """
    enabled = [t for t in (agent.enabled_tools or []) if t not in EXTERNAL_INTEGRATIONS]
    registry = ToolRegistry(db=db, user_id=user_id, workspace_id=workspace_id)
    definitions = registry.get_all_tool_definitions(
        enabled_tools=enabled,
        enabled_tool_ids=agent.enabled_tool_ids or None,
    )
    return BoundTools(registry, to_anthropic_tools(definitions))


__all__ = ["EXTERNAL_INTEGRATIONS", "BoundTools", "bind_agent_tools", "to_anthropic_tools"]
