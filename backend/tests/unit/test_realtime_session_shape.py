"""The session payload the bridge sends to OpenAI.

This is the one part of the call path that fails without any local error at all.
`session.update` casts its argument and `send` transforms it — neither
validates — so a session in the beta shape reaches the GA endpoint intact, is
rejected there as an asynchronous `error` event nothing is awaiting, and the
call proceeds on default settings: PCM16 at 24 kHz against a carrier sending
8 kHz mu-law, no instructions, no tools, and no `session.updated` to release the
greeting. The caller hears nothing and every log line reads healthy.

These tests assert against the SDK's own GA model, which is the schema the
server enforces.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.realtime.realtime_session_create_request import (
    RealtimeSessionCreateRequest,
)

from app.services.gpt_realtime import GPTRealtimeSession, _assert_ga_session

MULAW = {"type": "audio/pcmu"}


class TestGaSessionGuard:
    def test_a_ga_session_passes(self) -> None:
        _assert_ga_session(
            {
                "type": "realtime",
                "instructions": "be brief",
                "output_modalities": ["audio"],
                "audio": {"output": {"format": MULAW, "voice": "marin"}},
            }
        )

    @pytest.mark.parametrize(
        "field",
        ["modalities", "input_audio_format", "output_audio_format", "voice", "temperature"],
    )
    def test_every_beta_field_is_rejected(self, field: str) -> None:
        """The model allows extra keys, so validation alone would pass these."""
        with pytest.raises(ValueError, match="GA realtime session"):
            _assert_ga_session({"type": "realtime", field: "whatever"})

    def test_a_missing_type_is_rejected(self) -> None:
        """`type` is what the beta shape omits, and it is required."""
        with pytest.raises(ValueError, match="type"):
            _assert_ga_session({"instructions": "hi"})


class TestConfiguredSession:
    """The payload `_configure_session` actually builds, captured off the wire."""

    @pytest.fixture
    def sent(self) -> dict[str, Any]:
        return {}

    @pytest.fixture
    def session(self, sent: dict[str, Any]) -> GPTRealtimeSession:
        realtime = GPTRealtimeSession(
            db=MagicMock(),
            user_id=1,
            agent_config={
                "system_prompt": "Book appointments.",
                "voice": "marin",
                "enabled_tools": [],
                # Carried by agents but absent from a GA session; it must not be
                # smuggled through.
                "temperature": 0.9,
            },
            workspace_id=None,
        )
        registry = MagicMock()
        registry.get_all_tool_definitions.return_value = []
        realtime.tool_registry = registry

        async def capture(*, session: dict[str, Any]) -> None:
            sent.update(session)

        connection = MagicMock()
        connection.session.update = AsyncMock(side_effect=capture)
        realtime.connection = connection
        return realtime

    @pytest.mark.asyncio
    async def test_it_is_a_valid_ga_session(
        self, session: GPTRealtimeSession, sent: dict[str, Any]
    ) -> None:
        await session._configure_session()

        assert set(sent) <= set(RealtimeSessionCreateRequest.model_fields), sent
        RealtimeSessionCreateRequest.model_validate(sent)

    @pytest.mark.asyncio
    async def test_both_directions_are_mulaw(
        self, session: GPTRealtimeSession, sent: dict[str, Any]
    ) -> None:
        """The carrier fixes this at 8 kHz mu-law; a default of PCM16 is silence."""
        await session._configure_session()

        assert sent["audio"]["input"]["format"] == MULAW
        assert sent["audio"]["output"]["format"] == MULAW

    @pytest.mark.asyncio
    async def test_audio_only_output(
        self, session: GPTRealtimeSession, sent: dict[str, Any]
    ) -> None:
        """GA takes one modality, and the transcript arrives with the audio."""
        await session._configure_session()

        assert sent["output_modalities"] == ["audio"]

    @pytest.mark.asyncio
    async def test_temperature_is_not_sent(
        self, session: GPTRealtimeSession, sent: dict[str, Any]
    ) -> None:
        await session._configure_session()

        assert "temperature" not in sent
