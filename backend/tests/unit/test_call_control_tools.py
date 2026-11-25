"""The call-control tools the model is offered, and the two it no longer is.

``transfer_call`` and ``send_dtmf`` returned ``success: True`` and nothing in the
bridge acted on either, so an agent told a caller "transferring you now" and the
caller stayed on the line. They are gone until they act through the carrier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.services.gpt_realtime import GPTRealtimeSession
from app.services.tools.call_control_tools import CallControlTools
from app.services.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

REMOVED = ("transfer_call", "send_dtmf")


def _names(definitions: list[dict[str, Any]]) -> list[str]:
    return [d.get("name") or d["function"]["name"] for d in definitions]


class TestOfferedTools:
    def test_only_end_call_is_defined(self) -> None:
        assert _names(CallControlTools.get_tool_definitions()) == ["end_call"]

    def test_end_call_no_longer_suggests_a_transfer_happened(self) -> None:
        (end_call,) = CallControlTools.get_tool_definitions()

        assert "transferred" not in str(end_call)

    def test_the_registry_offers_only_end_call(self, test_session: AsyncSession) -> None:
        registry = ToolRegistry(test_session, 1)

        assert registry.enabled_tool_names(["call_control"]) == {"end_call"}

    def test_an_agent_that_saved_the_removed_tools_still_loads(
        self, test_session: AsyncSession
    ) -> None:
        """A saved selection naming them must not raise, and must not offer them."""
        registry = ToolRegistry(test_session, 1)
        saved = {"call_control": ["end_call", *REMOVED]}

        definitions = registry.get_all_tool_definitions(["call_control"], saved)

        assert _names(definitions) == ["end_call"]

    def test_a_selection_of_only_the_removed_tools_offers_nothing(
        self, test_session: AsyncSession
    ) -> None:
        registry = ToolRegistry(test_session, 1)

        names = registry.enabled_tool_names(["call_control"], {"call_control": list(REMOVED)})

        assert names == set()


class TestRemovedToolsNeverReportSuccess:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", REMOVED)
    async def test_the_executor_refuses_them(self, name: str) -> None:
        result = await CallControlTools.execute_tool(
            name, {"destination": "+15550001111", "digits": "1"}
        )

        assert result["success"] is False
        assert "action" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", REMOVED)
    async def test_the_registry_does_not_route_them_as_call_control(
        self, test_session: AsyncSession, name: str
    ) -> None:
        registry = ToolRegistry(test_session, 1)

        result = await registry.execute_tool(name, {"destination": "+15550001111", "digits": "1"})

        assert result.get("success") is not True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", REMOVED)
    async def test_a_model_that_names_one_is_told_it_is_unavailable(
        self, test_session: AsyncSession, name: str
    ) -> None:
        """What the model emits is not limited to what it was offered; the bridge
        must answer a stale or invented name honestly, even for an agent whose
        saved configuration still lists it."""
        session = GPTRealtimeSession(
            test_session,
            1,
            {"enabled_tools": ["call_control"], "enabled_tool_ids": {"call_control": [name]}},
        )
        session.tool_registry = ToolRegistry(test_session, 1)

        result = await session.handle_tool_call(
            {"name": name, "arguments": {"destination": "+15550001111", "digits": "1"}}
        )

        assert result["success"] is False
        assert "not available" in result["error"]
