"""The phone leg against a stand-in for Twilio. No network beyond localhost, no carrier."""

import asyncio
import json
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from synthiq_caller import dtmf
from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director
from synthiq_caller.leg import AgentAudio, AgentText, Closed, Leg
from synthiq_caller.legs.fake_twilio import RATE, FakeTwilio, tone_pcm
from synthiq_caller.legs.twilio_media import CallNotConnected, PhoneConfig, TwilioMediaLeg
from synthiq_caller.mulaw import lin2ulaw, ulaw2lin
from synthiq_caller.phone_server import UsageError, agent_lines_for_dry_run, check_public_url, main
from synthiq_caller.scenario import parse_scenario
from synthiq_caller.transcribe import DeepgramTranscriber, ScriptedTranscriber
from synthiq_caller.turns import TurnSettings, rms
from synthiq_caller.twilio_rest import (
    Credentials,
    MissingCredentials,
    TwilioRest,
    expected_signature,
    stream_twiml,
    valid_signature,
)

FAST = TurnSettings(hangover_s=0.25, audio_start_wait_s=0.6, min_speech_s=0.06)
TOKEN = "test-auth-token"  # noqa: S105 - a test value, not a credential


async def make_leg(
    fake: FakeTwilio, *, lines: list[str] | None = None, answer_timeout_s: int = 60
) -> TwilioMediaLeg:
    leg = TwilioMediaLeg(
        PhoneConfig(
            to_number="+441111111111",
            from_number="+442222222222",
            listen_host="127.0.0.1",
            listen_port=0,
            answer_timeout_s=answer_timeout_s,
            stream_grace_s=0.0,
        ),
        fake,
        auth_token=TOKEN,
        transcriber=ScriptedTranscriber(lines) if lines is not None else None,
    )
    port = await leg.start_server()
    leg.public_url = f"http://127.0.0.1:{port}"
    return leg


def write_clip(directory: Path, name: str, seconds: float) -> None:
    with wave.open(str(directory / name), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(tone_pcm(seconds, frequency=300.0))


# ----- codec and tones --------------------------------------------------


def test_mulaw_round_trip_keeps_speech_and_silence_apart() -> None:
    speech = tone_pcm(0.1)
    decoded = ulaw2lin(lin2ulaw(speech))
    assert len(decoded) == len(speech)
    # Mu-law is lossy by design; the level must survive within a few percent.
    assert abs(rms(decoded) - rms(speech)) / rms(speech) < 0.05
    assert rms(ulaw2lin(lin2ulaw(b"\0" * 320))) < 20


@pytest.mark.parametrize("digit", list("0123456789*#"))
def test_each_key_carries_its_two_frequencies(digit: str) -> None:
    pcm = dtmf.tone(digit, RATE, 0.16)
    low, high = dtmf.FREQUENCIES[digit]
    powers = {f: dtmf.goertzel(pcm, RATE, f) for f in (*dtmf.LOW, *dtmf.HIGH)}
    loudest = sorted(powers, key=lambda f: powers[f], reverse=True)[:2]
    assert sorted(loudest) == [low, high]
    assert dtmf.detect(pcm, RATE) == digit
    # And it still reads correctly after the telephone codec.
    assert dtmf.detect(ulaw2lin(lin2ulaw(pcm)), RATE) == digit


def test_tones_reject_a_key_that_does_not_exist() -> None:
    with pytest.raises(dtmf.UnknownDigit):
        dtmf.tones("12x", RATE)
    assert dtmf.detect(tone_pcm(0.16), RATE) is None


# ----- signatures, TwiML, credentials -----------------------------------


def test_signature_is_checked_against_the_exact_url() -> None:
    url = "https://caller.example.com/status/abc?CallSid=CA1&CallStatus=completed"
    signature = expected_signature(TOKEN, url)
    assert valid_signature(TOKEN, url, signature)
    assert not valid_signature(TOKEN, url + "x", signature)
    assert not valid_signature("another-token", url, signature)
    assert not valid_signature(TOKEN, url, "")
    # A form POST signs the URL followed by each name and value in name order.
    posted = expected_signature(TOKEN, "https://h/x", {"b": "2", "a": "1"})
    assert posted == expected_signature(TOKEN, "https://h/xa1b2")


def test_twiml_connects_a_stream_and_carries_parameters_not_a_query() -> None:
    twiml = stream_twiml("wss://caller.example.com/media/t", {"token": 'a"b<c'})
    assert twiml.startswith("<Response><Connect><Stream ")
    assert 'url="wss://caller.example.com/media/t"' in twiml
    assert "<Parameter name=" in twiml and "&lt;" in twiml and "?" not in twiml


def test_credentials_name_exactly_what_is_missing() -> None:
    with pytest.raises(MissingCredentials) as missing:
        Credentials.from_env({"TWILIO_ACCOUNT_SID": "AC1"})
    assert "TWILIO_AUTH_TOKEN" in str(missing.value)
    assert "TWILIO_ACCOUNT_SID" not in str(missing.value)
    with pytest.raises(MissingCredentials):
        Credentials.from_env(
            {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "TWILIO_API_KEY_SID": "SK1"}
        )
    with_key = Credentials.from_env(
        {
            "TWILIO_ACCOUNT_SID": "AC1",
            "TWILIO_AUTH_TOKEN": "t",
            "TWILIO_API_KEY_SID": "SK1",
            "TWILIO_API_KEY_SECRET": "s",
        }
    )
    assert with_key.basic_auth() == ("SK1", "s")
    assert Credentials.from_env(
        {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t"}
    ).basic_auth() == ("AC1", "t")


async def test_rest_client_sends_the_call_as_twilio_expects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": "CA123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rest = TwilioRest(Credentials("AC1", "t"), client=client)
    sid = await rest.create_call(
        to="+441111111111",
        from_="+442222222222",
        twiml="<Response/>",
        status_callback="https://h/status/x",
        record=True,
        timeout_s=30,
    )
    await rest.end_call(sid)
    await rest.aclose()

    create, end = seen
    assert sid == "CA123"
    assert str(create.url) == "https://api.twilio.com/2010-04-01/Accounts/AC1/Calls.json"
    body = create.content.decode()
    assert "StatusCallbackMethod=GET" in body
    assert body.count("StatusCallbackEvent=") == 4
    assert "Record=true" in body and "RecordingChannels=dual" in body
    assert create.headers["authorization"].startswith("Basic ")
    assert str(end.url).endswith("/Calls/CA123.json") and b"Status=completed" in end.content


# ----- the leg against the stand-in -------------------------------------


def test_the_phone_leg_satisfies_the_leg_protocol() -> None:
    leg = TwilioMediaLeg(
        PhoneConfig(to_number="+441", from_number="+442"), FakeTwilio([]), auth_token=TOKEN
    )
    assert isinstance(leg, Leg)
    assert leg.input_rate == 8000


async def collect(leg: TwilioMediaLeg, into: list[object]) -> None:
    async for event in leg.events():
        into.append(event)


async def test_lifecycle_audio_in_text_from_transcriber_and_far_end_hang_up() -> None:
    fake = FakeTwilio(agent_lines=[0.3], patience_s=0.3)
    leg = await make_leg(fake, lines=["How can I help?"])
    await leg.connect()
    events: list[object] = []
    await asyncio.wait_for(collect(leg, events), timeout=5)
    await leg.close()

    assert leg.call_ref == fake.created[0]["sid"]
    assert leg.log.stream_sid.startswith("MZ")
    audio = [e for e in events if isinstance(e, AgentAudio)]
    assert audio and all(e.rate == 8000 for e in audio)
    assert any(rms(e.pcm) > 250 for e in audio), "the agent's speech must arrive as PCM"
    assert [e.text for e in events if isinstance(e, AgentText)] == ["How can I help?"]
    assert isinstance(events[-1], Closed) and "far end" in events[-1].reason
    # The far end ended it, so the leg must not try to end it again.
    assert fake.ended == []
    assert fake.created[0]["record"] is True


async def test_outbound_audio_is_paced_at_twenty_milliseconds() -> None:
    fake = FakeTwilio(agent_lines=[0.1], patience_s=3.0)
    leg = await make_leg(fake)
    await leg.connect()
    seconds = 1.0
    await leg.send_pcm(tone_pcm(seconds))  # all at once: pacing is the leg's job
    await asyncio.sleep(seconds + 0.5)
    await leg.close()
    await fake.wait_finished()

    speech = [f for f in fake.frames if f.is_speech]
    assert all(len(f.mulaw) == 160 for f in fake.frames), "one 20 ms frame per message"
    assert abs(len(speech) - 50) <= 2
    span = speech[-1].t - speech[0].t
    per_frame = span / (len(speech) - 1)
    assert 0.018 <= per_frame <= 0.023, f"mean spacing {per_frame * 1000:.1f} ms"
    assert span < seconds + 0.15, "a queued second must not be sent in a burst or stretched"


async def test_silence_is_not_sent_unless_asked_for() -> None:
    fake = FakeTwilio(agent_lines=[0.1], patience_s=1.5)
    leg = await make_leg(fake)
    await leg.connect()
    await leg.send_pcm(b"\0" * 3200)
    await asyncio.sleep(0.4)
    await leg.close()
    await fake.wait_finished()
    assert fake.frames == []
    assert leg.log.frames_skipped_silent >= 9


async def test_clear_drops_what_is_queued_and_tells_the_carrier() -> None:
    fake = FakeTwilio(agent_lines=[0.1], patience_s=3.0)
    leg = await make_leg(fake)
    await leg.connect()
    await leg.send_pcm(tone_pcm(2.0))
    await asyncio.sleep(0.3)
    await leg.clear_playback()
    await asyncio.sleep(0.4)
    await leg.close()
    await fake.wait_finished()

    assert len(fake.clears) == 1 and leg.log.clears_sent == 1
    after = [f for f in fake.frames if f.t > fake.clears[0] + 0.1]
    assert after == [], "nothing queued before the clear may be sent after it"
    assert len(fake.frames) < 40, "two seconds were queued; most must have been dropped"


async def test_keypad_digits_go_out_as_tones_the_far_end_can_decode() -> None:
    fake = FakeTwilio(agent_lines=[0.1], patience_s=3.0)
    leg = await make_leg(fake)
    await leg.connect()
    await leg.send_dtmf("19#")
    await asyncio.sleep(1.1)
    await leg.close()
    await fake.wait_finished()

    decoded: list[str] = []
    for start, end in fake.caller_speech():
        burst = b"".join(f.mulaw for f in fake.frames if start <= f.t < end)
        key = dtmf.detect(ulaw2lin(burst), RATE)
        assert key is not None
        decoded.append(key)
    assert decoded == ["1", "9", "#"]


async def test_a_stream_with_the_wrong_token_is_refused() -> None:
    fake = FakeTwilio(agent_lines=[0.2], corrupt_token=True)
    leg = await make_leg(fake, answer_timeout_s=1)
    with pytest.raises(CallNotConnected):
        await leg.connect()
    await fake.wait_finished()
    assert fake.close_code == 4401
    assert any("token" in reason for reason in leg.log.rejected)
    assert fake.ended == [fake.created[0]["sid"]], "an unconnected call must still be ended"


async def test_an_unanswered_call_fails_loudly_and_is_ended() -> None:
    fake = FakeTwilio(agent_lines=[], answer=False)
    leg = TwilioMediaLeg(
        PhoneConfig(
            to_number="+441",
            from_number="+442",
            listen_host="127.0.0.1",
            listen_port=0,
            answer_timeout_s=1,
            stream_grace_s=0.0,
        ),
        fake,
        auth_token=TOKEN,
    )
    port = await leg.start_server()
    leg.public_url = f"http://127.0.0.1:{port}"
    with pytest.raises(CallNotConnected, match="never started"):
        await leg.connect()
    assert fake.ended == [fake.created[0]["sid"]]


async def test_connect_refuses_without_a_public_address() -> None:
    leg = TwilioMediaLeg(
        PhoneConfig(to_number="+441", from_number="+442"), FakeTwilio([]), auth_token=TOKEN
    )
    with pytest.raises(CallNotConnected, match="public_url"):
        await leg.connect()


async def test_status_callback_needs_a_valid_signature() -> None:
    fake = FakeTwilio(agent_lines=[0.1], patience_s=5.0)
    leg = await make_leg(fake)
    await leg.connect()
    base = leg.status_url()
    query = f"?CallSid={leg.call_ref}&CallStatus=completed"

    async with httpx.AsyncClient() as client:
        unsigned = await client.get(base + query)
        forged = await client.get(base + query, headers={"X-Twilio-Signature": "AAAA"})
        elsewhere = await client.get(
            leg.public_url + "/status/not-the-token" + query,
            headers={
                "X-Twilio-Signature": expected_signature(
                    TOKEN, leg.public_url + "/status/not-the-token" + query
                )
            },
        )
        assert (unsigned.status_code, forged.status_code) == (403, 403)
        assert elsewhere.status_code == 404
        assert leg.log.statuses == []

        signed = await client.get(
            base + query, headers={"X-Twilio-Signature": expected_signature(TOKEN, base + query)}
        )
    assert signed.status_code == 200
    assert leg.log.statuses == ["completed"]

    events: list[object] = []
    await asyncio.wait_for(collect(leg, events), timeout=3)
    closed = [e for e in events if isinstance(e, Closed)]
    assert closed and closed[0].reason == "(call completed)"
    await leg.close()
    await fake.wait_finished()
    assert fake.ended == [], "a call the carrier reports as over is not ended twice"


# ----- end to end through the director ----------------------------------


async def test_a_scenario_runs_end_to_end_over_the_phone_leg(tmp_path: Path) -> None:
    write_clip(tmp_path, "opener.wav", 0.4)
    write_clip(tmp_path, "dob.wav", 0.5)
    scenario = parse_scenario(
        {
            "name": "phone: opener then date of birth",
            "persona": {"facts": {"date_of_birth": "1987-07-05"}},
            "steps": [
                {"when": "how can i help", "clips": ["opener.wav"]},
                {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
            ],
            "hangup_after": "mobile number",
            "max_seconds": 20,
        }
    )
    lines = ["Hi, how can I help?", "What is your date of birth?", "And your mobile number?"]
    fake = FakeTwilio(agent_lines=[0.5, 0.5, 0.4], patience_s=4.0)
    leg = await make_leg(fake, lines=lines)

    record = await Director(
        scenario,
        leg,
        ClipBank(tmp_path, allow_unknown_provenance=True, allow_missing_consent=True),
        turn_settings=FAST,
    ).run()
    await fake.wait_finished()

    assert record.ended == "caller hung up: scenario end"
    assert record.call_ref == fake.created[0]["sid"]
    assert [s.clips for s in record.said] == [["opener.wav"], ["dob.wav"]]
    spoken = fake.caller_speech()
    assert len(spoken) == 2
    assert 0.3 <= spoken[0][1] - spoken[0][0] <= 0.6
    assert 0.4 <= spoken[1][1] - spoken[1][0] <= 0.7
    for said_start, _ in spoken:
        for agent_start, agent_end in fake.agent_speech:
            assert not agent_start < said_start < agent_end, "the caller spoke over the agent"
    assert fake.ended == [record.call_ref], "the caller hangs up by ending the call"


# ----- transcribers ------------------------------------------------------


async def test_scripted_transcriber_emits_one_line_per_stretch_of_speech() -> None:
    said: list[str] = []
    transcriber = ScriptedTranscriber(["first", "second"], quiet_s=0.2)
    await transcriber.start(said.append)
    speech = lin2ulaw(tone_pcm(0.02))
    quiet = lin2ulaw(b"\0" * 320)
    for frame in [speech] * 10 + [quiet] * 3 + [speech] * 5 + [quiet] * 15 + [speech] * 5:
        await transcriber.feed(frame)
    # The short gap is one utterance; only the long gap starts a new line.
    assert said == ["first", "second"]
    for frame in [quiet] * 15 + [speech] * 5:
        await transcriber.feed(frame)
    assert said == ["first", "second"], "no lines left: nothing more is emitted"


class FakeDeepgramSocket:
    def __init__(self, messages: list[str]) -> None:
        self.sent: list[str | bytes] = []
        self.closed = False
        self._messages = messages
        self.release = asyncio.Event()

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str | bytes]:
        for message in self._messages:
            yield message
        await self.release.wait()


def result(transcript: str, *, final: bool) -> str:
    return json.dumps(
        {
            "type": "Results",
            "is_final": final,
            "speech_final": final,
            "channel": {"alternatives": [{"transcript": transcript, "confidence": 0.9}]},
        }
    )


async def test_deepgram_transcriber_speaks_the_live_protocol() -> None:
    socket = FakeDeepgramSocket(
        [
            json.dumps({"type": "Metadata", "request_id": "r"}),
            result("could i take", final=False),
            result("Could I take your date of birth?", final=True),
            result("", final=True),
            "not json",
            json.dumps({"type": "UtteranceEnd"}),
        ]
    )
    connected: list[tuple[str, dict[str, str]]] = []

    async def connect(url: str, headers: dict[str, str]) -> FakeDeepgramSocket:
        connected.append((url, headers))
        return socket

    said: list[str] = []
    transcriber = DeepgramTranscriber(
        "dg-key", keyterms=("MRI",), connect=connect, keepalive_s=0.05
    )
    await transcriber.start(said.append)
    await transcriber.feed(b"\xff" * 160)
    await asyncio.sleep(0.15)
    await transcriber.close()

    url, headers = connected[0]
    assert url.startswith("wss://api.deepgram.com/v1/listen?")
    for part in ("model=nova-3", "encoding=mulaw", "sample_rate=8000", "language=en-GB"):
        assert part in url
    assert "interim_results=false" in url and "keyterm=MRI" in url
    assert headers == {"Authorization": "Token dg-key"}
    assert said == ["Could I take your date of birth?"], "only non-empty final results"
    assert socket.sent[0] == b"\xff" * 160, "audio goes up as raw binary, unconverted"
    assert json.dumps({"type": "KeepAlive"}) in socket.sent
    assert socket.sent[-1] == json.dumps({"type": "CloseStream"}) and socket.closed


def test_deepgram_transcriber_needs_a_key() -> None:
    with pytest.raises(ValueError, match="key"):
        DeepgramTranscriber("")


# ----- command line -------------------------------------------------------


def scenario_file(directory: Path) -> Path:
    path = directory / "scenario.json"
    path.write_text(
        json.dumps(
            {
                "name": "dry run",
                "steps": [
                    {"when": "how can i help", "clips": ["opener.wav"]},
                    {"when": "date of birth", "dtmf": "05071987"},
                ],
                "hangup_after": "mobile number",
                "max_seconds": 20,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_refuses_to_start_without_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_clip(tmp_path, "opener.wav", 0.3)
    arguments = [
        "--scenario",
        str(scenario_file(tmp_path)),
        "--clips",
        str(tmp_path),
        "--out",
        str(tmp_path / "runs"),
        "--from",
        "+442222222222",
        "--to",
        "+441111111111",
        "--public-url",
        "https://caller.example.com",
    ]
    with pytest.raises(SystemExit) as stopped:
        main(arguments, env={"TWILIO_ACCOUNT_SID": "AC1"})
    assert stopped.value.code == 2
    message = capsys.readouterr().err
    assert "TWILIO_AUTH_TOKEN" in message and "AC1" not in message
    assert not (tmp_path / "runs").exists(), "nothing may be written when nothing ran"


def test_public_url_must_be_a_real_https_address() -> None:
    assert check_public_url("https://caller.example.com/") == "https://caller.example.com"
    for bad in ("", "http://caller.example.com", "caller.example.com", "https://h/x?y=1"):
        with pytest.raises(UsageError):
            check_public_url(bad)


def test_dry_run_places_no_call_and_prints_what_it_would_send(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_clip(tmp_path, "opener.wav", 0.4)
    path = scenario_file(tmp_path)
    assert agent_lines_for_dry_run(parse_scenario(json.loads(path.read_text())), None) == [
        "how can i help",
        "date of birth",
        "mobile number",
    ]
    code = main(
        [
            "--scenario",
            str(path),
            "--clips",
            str(tmp_path),
            "--out",
            str(tmp_path / "runs"),
            "--dry-run",
            "--allow-unknown-provenance",
            "--allow-missing-consent",
        ],
        env={},
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN: no call was placed" in printed
    assert "opener.wav" in printed and "keys 05071987" in printed
    assert "<Connect><Stream" in printed
    written = json.loads((tmp_path / "runs" / "phone-run-001.json").read_text())
    assert written["dry_run"] is True
    assert written["record"]["ended"] == "caller hung up: scenario end"
    assert written["phone"]["frames_sent"] > 20
