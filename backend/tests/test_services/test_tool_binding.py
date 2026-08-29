"""Tests for binding an agent's real tools into a simulated run.

The point of these is the difference between this harness and every simulator
that mocks tool responses: here the tool actually runs, so what the metrics
score is what the database did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.monitoring.call_trace import ToolOutcome
from app.services.qa.tool_binding import (
    BoundTools,
    bind_agent_tools,
    to_anthropic_tools,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _FakeRegistry:
    """Stands in for ToolRegistry so a test can choose what a tool returns."""

    def __init__(self, result: Any = None, raises: Exception | None = None) -> None:
        self.result = result if result is not None else {"success": True}
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if self.raises:
            raise self.raises
        return self.result

    async def close(self) -> None:
        self.closed = True


def _bound(registry: Any, *, required: list[str] | None = None) -> BoundTools:
    return BoundTools(
        registry,
        [
            {
                "name": "book_appointment",
                "description": "book",
                "input_schema": {
                    "type": "object",
                    "properties": {"contact_phone": {"type": "string"}},
                    "required": required or [],
                },
            }
        ],
    )


class TestToAnthropicTools:
    def test_converts_the_flat_realtime_shape(self) -> None:
        tools = to_anthropic_tools(
            [
                {
                    "type": "function",
                    "name": "search_customer",
                    "description": "find one",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        )
        assert tools == [
            {
                "name": "search_customer",
                "description": "find one",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]

    def test_converts_the_nested_chat_completions_shape(self) -> None:
        tools = to_anthropic_tools(
            [{"type": "function", "function": {"name": "ghl_get_contact", "description": "x"}}],
        )
        assert tools[0]["name"] == "ghl_get_contact"

    def test_drops_duplicate_names(self) -> None:
        """The registry emits the CRM tools once for `crm` and again for
        `bookings`; the API rejects a request naming the same tool twice."""
        tools = to_anthropic_tools(
            [
                {"name": "book_appointment", "description": "first"},
                {"name": "book_appointment", "description": "second"},
            ],
        )
        assert len(tools) == 1
        assert tools[0]["description"] == "first"

    def test_ignores_definitions_without_a_name(self) -> None:
        assert to_anthropic_tools([{"description": "nameless"}, "junk", None]) == []

    def test_supplies_an_empty_schema_when_none_is_declared(self) -> None:
        """A missing input_schema is a 400 from the API, not a tool with no
        arguments."""
        assert to_anthropic_tools([{"name": "end_call"}])[0]["input_schema"] == {
            "type": "object",
            "properties": {},
        }


@pytest.mark.asyncio
class TestBoundToolsExecute:
    async def test_executes_the_tool_and_records_success(self) -> None:
        registry = _FakeRegistry({"success": True, "appointment_id": "a1"})
        record = await _bound(registry).execute("book_appointment", {"contact_phone": "555"})

        assert registry.calls == [("book_appointment", {"contact_phone": "555"})]
        assert record["outcome"] == ToolOutcome.OK
        assert record["error"] is None
        assert record["duration_ms"] >= 0

    async def test_a_failing_tool_is_recorded_not_raised(self) -> None:
        """A tool that fails is a finding about the agent, not a crash of the
        harness - a run that dies here reports nothing at all."""
        registry = _FakeRegistry({"success": False, "error": "no such contact"})
        record = await _bound(registry).execute("book_appointment", {})

        assert record["outcome"] == ToolOutcome.ERROR
        assert record["error"] == "no such contact"

    async def test_an_exception_is_recorded_as_an_error(self) -> None:
        registry = _FakeRegistry(raises=RuntimeError("connection reset"))
        record = await _bound(registry).execute("book_appointment", {})

        assert record["outcome"] == ToolOutcome.ERROR
        assert "connection reset" in record["error"]

    async def test_missing_required_arguments_are_invalid_not_errors(self) -> None:
        """A malformed call is a model failure; running it anyway would turn it
        into a downstream error that reads like an outage."""
        registry = _FakeRegistry()
        record = await _bound(registry, required=["contact_phone"]).execute(
            "book_appointment",
            {},
        )

        assert record["outcome"] == ToolOutcome.INVALID_ARGS
        assert registry.calls == []

    async def test_an_empty_string_does_not_satisfy_a_required_argument(self) -> None:
        registry = _FakeRegistry()
        record = await _bound(registry, required=["contact_phone"]).execute(
            "book_appointment",
            {"contact_phone": ""},
        )
        assert record["outcome"] == ToolOutcome.INVALID_ARGS

    async def test_an_unbound_tool_name_is_rejected_without_executing(self) -> None:
        registry = _FakeRegistry()
        record = await _bound(registry).execute("wire_money", {})

        assert record["outcome"] == ToolOutcome.ERROR
        assert registry.calls == []

    async def test_close_releases_the_registry(self) -> None:
        registry = _FakeRegistry()
        await _bound(registry).close()
        assert registry.closed is True

    async def test_records_feed_the_metric_context_unchanged(self) -> None:
        """The record shape is the contract between this module and the
        metrics; if it drifts, tool metrics go silently unmeasurable."""
        from app.services.qa.metrics.context import tool_calls_from_records

        registry = _FakeRegistry({"success": False, "error": "boom"})
        record = await _bound(registry).execute("book_appointment", {"contact_phone": "5"})

        calls = tool_calls_from_records([record])
        assert calls[0].name == "book_appointment"
        assert calls[0].outcome is ToolOutcome.ERROR
        assert calls[0].arguments == {"contact_phone": "5"}


@pytest.mark.asyncio
class TestBindAgentTools:
    async def test_binds_the_agents_crm_tools(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        bound = await bind_agent_tools(test_session, agent, user.id)

        assert "book_appointment" in bound.names
        assert "create_contact" in bound.names

    async def test_excludes_third_party_integrations(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """A test run must never text a real customer or write into someone's
        live calendar."""
        user = await create_test_user()
        agent = await create_test_agent(
            user_id=user.id,
            enabled_tools=["crm", "telnyx-sms", "gohighlevel"],
        )

        bound = await bind_agent_tools(test_session, agent, user.id)

        assert not any(name.startswith(("telnyx_", "ghl_")) for name in bound.names)

    async def test_an_agent_with_no_tools_binds_nothing(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=[])

        bound = await bind_agent_tools(test_session, agent, user.id)

        assert bound.names == ()
        assert not bound

    async def test_crm_and_bookings_together_do_not_duplicate_tools(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm", "bookings"])

        bound = await bind_agent_tools(test_session, agent, user.id)

        assert len(bound.names) == len(set(bound.names))
