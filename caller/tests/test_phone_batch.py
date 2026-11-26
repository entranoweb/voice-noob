"""The phone leg inside a batch, against stand-ins. No carrier, no network beyond localhost."""

import json
import socket
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from conftest import mark_checked

from synthiq_caller import cli
from synthiq_caller.batch import CallInfo, regrade, reread_missing_traces, run_batch
from synthiq_caller.clips import ClipBank
from synthiq_caller.leg import Leg
from synthiq_caller.legs.fake_twilio import RATE, FakeTwilio, tone_pcm
from synthiq_caller.legs.twilio_media import (
    NumberNotAllowed,
    PhoneConfig,
    PhoneExchange,
    TwilioMediaLeg,
)
from synthiq_caller.numbers import ALLOWED_ENV, allowed_numbers, is_allowed, same_number
from synthiq_caller.phone_batch import phone_leg_factory, phone_preflight, render_preflight
from synthiq_caller.phone_server import main as phone_main
from synthiq_caller.recording import CallRecorder
from synthiq_caller.report import Summary, fail_under_exit, render, summarise
from synthiq_caller.scenario import Scenario, parse_scenario
from synthiq_caller.traces.base import ToolCall, Trace, TraceReader
from synthiq_caller.traces.lookup import (
    ElevenLabsNumberTimeFinder,
    LookupTraceReader,
    PhoneCall,
    TraceNotFound,
    calls_in,
)
from synthiq_caller.transcribe import ScriptedTranscriber
from synthiq_caller.turns import TurnSettings, rms

FAST = TurnSettings(hangover_s=0.25, audio_start_wait_s=0.6, min_speech_s=0.06)
TOKEN = "test-auth-token"  # noqa: S105 - a test value, not a credential
TO, FROM = "+441111111111", "+442222222222"
LINES = ["Hi, how can I help?", "What is your date of birth?", "And your mobile number?"]


def scenario(name: str = "phone lookup", caller: str = "Alice Wonderland") -> Scenario:
    return parse_scenario(
        {
            "name": name,
            "mutates": "none",
            "persona": {"facts": {"name": caller, "date_of_birth": "1987-07-05"}},
            "steps": [
                {"when": "how can i help", "clips": ["opener.wav"]},
                {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
            ],
            "hangup_after": "mobile number",
            "max_seconds": 20,
            "expect": {
                "tools": [{"name_contains": "new_patient_check"}],
                "fields": ["date_of_birth"],
            },
        }
    )


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    directory = tmp_path / "bank"
    directory.mkdir()
    for name, seconds in (("opener.wav", 0.4), ("dob.wav", 0.5)):
        with wave.open(str(directory / name), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(tone_pcm(seconds, frequency=300.0))
    mark_checked(directory)
    return ClipBank(directory)


class StoredDate:
    """The agent platform's trace reader: one tool call carrying the date it stored."""

    def __init__(self, stored: str = "1987-07-05") -> None:
        self.stored = stored
        self.asked: list[str] = []

    async def read(self, call_ref: str) -> Trace:
        self.asked.append(call_ref)
        call = ToolCall(
            "example_v2_new_patient_check", {"dateOfBirth": self.stored}, {}, True, "NP"
        )
        return Trace(call_ref=call_ref, tool_calls=[call], step_path=["I1", "NP_CHECK"])


class FinderByOrder:
    """Finds conversation ``conv-N`` for the Nth call it is asked about, or nothing."""

    def __init__(self, *, finds: bool = True) -> None:
        self.finds = finds
        self.asked: list[PhoneCall] = []

    async def find(self, call: PhoneCall) -> str:
        self.asked.append(call)
        if not self.finds:
            raise TraceNotFound("trace not found: no inbound conversation in the window")
        return f"conv-{len(self.asked)}"


async def no_wait(_: float) -> None:
    return None


class Line:
    """A shared exchange on localhost, and a fresh stand-in carrier for every call."""

    def __init__(self, tmp_path: Path, finder: FinderByOrder, inner: TraceReader | None) -> None:
        self.exchange = PhoneExchange("127.0.0.1", 0)
        self.fakes: list[FakeTwilio] = []
        self.out = tmp_path / "runs"
        self._finder = finder
        self._inner = inner

    def carrier(self) -> FakeTwilio:
        fake = FakeTwilio(agent_lines=[0.5, 0.5, 0.4], patience_s=4.0)
        self.fakes.append(fake)
        return fake

    async def factory(self, **config: Any) -> Any:
        port = await self.exchange.start()
        make = phone_leg_factory(
            config=PhoneConfig(
                to_number=TO,
                from_number=FROM,
                stream_grace_s=0.0,
                allowed_to=(TO,),
                audio_dir=str(self.out / "audio"),
                **config,
            ),
            public_url=lambda: f"http://127.0.0.1:{port}",
            control=self.carrier,
            auth_token=TOKEN,
            transcriber=lambda: ScriptedTranscriber(list(LINES)),
            exchange=self.exchange,
            finder=self._finder,
            inner_reader=self._inner,
        )

        def patient() -> tuple[Leg, TraceReader | None]:
            leg, reader = make()
            if isinstance(reader, LookupTraceReader):
                reader._wait = no_wait  # do not sit out the lookup's retries in a test
            return leg, reader

        return patient


def pcm_of(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (RATE, 1, 2)
        return wav.readframes(wav.getnframes())


# ----- a batch over the phone ---------------------------------------------------


async def test_a_phone_batch_is_graded_like_a_socket_batch_through_one_server(
    bank: ClipBank, tmp_path: Path
) -> None:
    finder, inner = FinderByOrder(), StoredDate()
    line = Line(tmp_path, finder, inner)
    try:
        result = await run_batch(
            [("lookup.json", scenario())],
            await line.factory(),
            bank,
            line.out,
            repeats=2,
            turn_settings=FAST,
            leg_name="phone",
        )
    finally:
        await line.exchange.close()

    assert result.ran == 2 and result.blocked == []
    runs = [json.loads(p.read_text()) for p in sorted(line.out.glob("run-*.json"))]
    # Two calls, two stand-in carriers, one listening port.
    ports = {fake.created[0]["status_callback"].split("/")[2] for fake in line.fakes}
    assert len(line.fakes) == 2 and len(ports) == 1
    for index, run in enumerate(runs, start=1):
        fake = line.fakes[index - 1]
        assert run["leg"] == "phone"
        assert run["phone"]["to_number"] == TO and run["phone"]["from_number"] == FROM
        assert run["phone"]["dialled_at_unix"] > 0 and run["phone"]["ended_at_unix"] > 0
        # The record keeps the carrier's id; the trace was read under the platform's.
        assert run["record"]["call_ref"] == fake.created[0]["sid"]
        assert run["trace"]["call_ref"] == f"conv-{index}"
        assert run["grade"]["outcome"] == "passed"
        row = run["grade"]["fields"][0]
        assert row["field"] == "date_of_birth" and row["stored_matches"] is True
    # The finder was told what the caller knew: the numbers and the time of each call.
    assert [(c.to_number, c.from_number) for c in finder.asked] == [(TO, FROM)] * 2
    assert all(c.carrier_ref.startswith("CA") and c.dialled_at_unix > 0 for c in finder.asked)
    assert inner.asked == ["conv-1", "conv-2"]

    summary = summarise(line.out)
    assert (summary.passed, summary.failed, summary.ungraded) == (2, 0, 0)
    assert summary.legs == {"phone": 2}
    assert "by line: phone 2" in render(summary)
    code, line_text = fail_under_exit(summary, 95)
    assert code == 0 and "2 of 2 calls passed" in line_text


async def test_each_side_of_a_phone_call_is_kept_as_its_own_recording(
    bank: ClipBank, tmp_path: Path
) -> None:
    line = Line(tmp_path, FinderByOrder(), StoredDate())
    try:
        await run_batch(
            [("lookup.json", scenario())],
            await line.factory(),
            bank,
            line.out,
            turn_settings=FAST,
        )
    finally:
        await line.exchange.close()

    run = json.loads((line.out / "run-001.json").read_text())
    audio = run["audio"]
    sid = run["record"]["call_ref"]
    assert audio["caller"] == f"audio/{sid}.caller.wav"
    assert audio["agent"] == f"audio/{sid}.agent.wav"
    caller, agent = pcm_of(line.out / audio["caller"]), pcm_of(line.out / audio["agent"])
    assert len(caller) == len(agent), "the two sides share one clock and one length"
    assert abs(len(caller) / 2 / RATE - audio["seconds"]) < 0.01

    def loud(pcm: bytes, start_s: float, end_s: float) -> bool:
        return rms(pcm[int(start_s * RATE) * 2 : int(end_s * RATE) * 2]) > 250

    fake = line.fakes[0]
    # Where the stand-in agent spoke, only the agent's file has sound.
    first_start, first_end = fake.agent_speech[0]
    assert loud(agent, first_start + 0.1, first_end - 0.1)
    assert not loud(caller, first_start + 0.1, first_end - 0.1)
    # Where the caller spoke, the caller's file has sound, at the time the far end heard it.
    said_start, said_end = fake.caller_speech(merge_gap_s=0.5)[0]
    assert loud(caller, said_start + 0.1, said_end - 0.1)
    assert not loud(agent, said_start + 0.1, said_end - 0.1)


async def test_a_call_whose_trace_is_not_found_is_ungraded_and_fails_the_gate(
    bank: ClipBank, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line = Line(tmp_path, FinderByOrder(finds=False), StoredDate())
    try:
        await run_batch(
            [("lookup.json", scenario())],
            await line.factory(),
            bank,
            line.out,
            turn_settings=FAST,
            wait=no_wait,
        )
    finally:
        await line.exchange.close()

    run = json.loads((line.out / "run-001.json").read_text())
    assert run["trace"] is None and "trace not found" in run["trace_error"]
    assert run["grade"]["outcome"] == "ungraded"
    summary = summarise(line.out)
    assert (summary.passed, summary.ungraded) == (0, 1)
    assert summary.ungraded_reasons == {"trace not found": 1}
    assert [left.outcome for left in summary.excluded] == ["ungraded"]
    assert "trace not found" in summary.excluded[0].reason
    assert "ungraded: TraceNotFound('trace not found" in render(summary)

    # An ungraded call counts against the gate; it must not slip past it.
    assert cli.report(str(line.out), details=False, fail_under=50) == 1
    assert "NOT met" in capsys.readouterr().out

    # Later the platform lists the call. The stored run says enough to find it.
    found = LookupTraceReader(
        FinderByOrder(), StoredDate(), calls_in(line.out), find_wait_s=0.0, wait=no_wait
    )
    assert await reread_missing_traces(line.out, found) == 1
    assert regrade(line.out, [("lookup.json", scenario())]) == 1
    assert cli.report(str(line.out), details=False, fail_under=100) == 0


class RememberingHooks:
    def __init__(self) -> None:
        self.calls: list[CallInfo | None] = []

    async def before_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None:
        return None

    async def after_call(
        self, scenario_file: str, scenario: Scenario, repeat: int, call: CallInfo | None = None
    ) -> None:
        self.calls.append(call)


@pytest.mark.parametrize(("finds", "expected"), [(True, "conv-1"), (False, "")])
async def test_the_after_call_hook_is_told_the_platforms_id_never_the_carriers(
    bank: ClipBank, tmp_path: Path, finds: bool, expected: str
) -> None:
    line = Line(tmp_path, FinderByOrder(finds=finds), StoredDate())
    hooks = RememberingHooks()
    try:
        await run_batch(
            [("lookup.json", scenario())],
            await line.factory(),
            bank,
            line.out,
            turn_settings=FAST,
            hooks=hooks,
            wait=no_wait,
        )
    finally:
        await line.exchange.close()

    run = json.loads((line.out / "run-001.json").read_text(encoding="utf-8"))
    told = hooks.calls[0]
    assert told is not None and told.run_file == "run-001.json"
    # A hook that tidies up by conversation id must get the platform's id, or none:
    # the carrier's id names a different leg of the call and would match nothing.
    assert told.call_ref == expected
    assert run["carrier_call_ref"] == run["record"]["call_ref"] != told.call_ref


def test_the_threshold_counts_every_call_but_those_the_rig_spoiled() -> None:
    assert fail_under_exit(Summary(runs=20, passed=19, failed=1), 95)[0] == 0
    assert fail_under_exit(Summary(runs=20, passed=18, failed=2), 95)[0] == 1
    # 18 passed of 18 graded, but two calls have no verdict: not 95% of what was placed.
    assert fail_under_exit(Summary(runs=20, passed=18, ungraded=2), 95)[0] == 1
    assert fail_under_exit(Summary(runs=20, passed=18, ungradeable=2), 95)[0] == 1
    # A call the rig spoiled is re-run, not held against the agent, and not a pass either.
    code, text = fail_under_exit(Summary(runs=20, passed=19, void=1), 95)
    assert code == 0 and "19 of 19" in text and "1 void left out" in text
    assert fail_under_exit(Summary(runs=2, void=2), 95)[0] == 1, "only void calls show nothing"
    assert fail_under_exit(Summary(), 95)[0] == 1, "nothing placed shows nothing"
    assert fail_under_exit(Summary(runs=3, failed=3), None) == (0, "")


async def test_an_unanswered_phone_call_is_a_recorded_failure_and_the_batch_goes_on(
    bank: ClipBank, tmp_path: Path
) -> None:
    line = Line(tmp_path, FinderByOrder(), StoredDate())
    answers = iter([False, True])

    def carrier() -> FakeTwilio:
        fake = FakeTwilio(agent_lines=[0.5, 0.5, 0.4], patience_s=4.0, answer=next(answers))
        line.fakes.append(fake)
        return fake

    line.carrier = carrier  # type: ignore[method-assign]
    try:
        result = await run_batch(
            [("lookup.json", scenario())],
            await line.factory(answer_timeout_s=1),
            bank,
            line.out,
            repeats=2,
            turn_settings=FAST,
            wait=no_wait,
        )
    finally:
        await line.exchange.close()

    assert result.ran == 2, "the call nobody answered must not stop the one after it"
    first, second = (
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(line.out.glob("run-*.json"))
    )
    assert first["record"]["ended"].startswith("not answered")
    assert (first["grade"]["outcome"], first["grade"]["fault"]) == ("failed", "unknown")
    assert first["leg"] == "phone" and first["phone"]["to_number"] == TO
    assert first["trace"] is None, "no trace is looked for when nobody answered"
    assert second["grade"]["outcome"] == "passed"
    summary = summarise(line.out)
    assert (summary.passed, summary.failed, summary.runs) == (1, 1, 2)
    assert fail_under_exit(summary, 95)[0] == 1


# ----- finding the trace --------------------------------------------------------


def conversation(conversation_id: str, **phone: Any) -> dict[str, Any]:
    call = {"type": "twilio", "direction": "inbound", "agent_number": TO, "external_number": FROM}
    return {
        "conversation_id": conversation_id,
        "metadata": {"start_time_unix_secs": 1000, "phone_call": {**call, **phone}},
    }


def elevenlabs(details: list[dict[str, Any]], seen: list[httpx.Request]) -> httpx.AsyncClient:
    by_id = {d["conversation_id"]: d for d in details}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        last = request.url.path.rsplit("/", 1)[-1]
        if last == "conversations":
            listed = [{"conversation_id": i, "start_time_unix_secs": 1000} for i in by_id]
            return httpx.Response(200, json={"conversations": listed, "has_more": False})
        return httpx.Response(200, json=by_id[last])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


CALL = PhoneCall("CA1", TO, FROM, dialled_at_unix=1000.0, ended_at_unix=1060.0)


async def test_the_finder_matches_on_both_numbers_the_direction_and_the_time() -> None:
    seen: list[httpx.Request] = []
    client = elevenlabs(
        [
            conversation("someone-else", external_number="+443333333333"),
            conversation("another-line", agent_number="+445555555555"),
            conversation("outbound", direction="outbound"),
            {"conversation_id": "web-chat", "metadata": {}},
            conversation("ours", agent_number="+44 1111 111111"),
        ],
        seen,
    )
    finder = ElevenLabsNumberTimeFinder("key", agent_id="agent_1", window_s=90, client=client)
    assert await finder.find(CALL) == "ours"

    listing = seen[0]
    assert listing.url.path.endswith("/convai/conversations")
    assert listing.url.params["agent_id"] == "agent_1"
    assert listing.url.params["call_start_after_unix"] == "910"
    assert listing.url.params["call_start_before_unix"] == "1150"
    assert listing.headers["xi-api-key"] == "key"
    assert all(request.method == "GET" for request in seen)


async def test_the_finder_never_guesses_between_two_calls_or_settles_for_none() -> None:
    twice = elevenlabs([conversation("a"), conversation("b")], [])
    with pytest.raises(TraceNotFound, match="refusing to guess"):
        await ElevenLabsNumberTimeFinder("key", client=twice).find(CALL)

    nobody = elevenlabs([conversation("other", external_number="+443333333333")], [])
    with pytest.raises(TraceNotFound, match="trace not found: no inbound conversation"):
        await ElevenLabsNumberTimeFinder("key", client=nobody).find(CALL)

    # A line that withholds the caller's number can still be matched, when asked to.
    withheld = elevenlabs([conversation("hidden", external_number="anonymous")], [])
    lenient = ElevenLabsNumberTimeFinder("key", client=withheld, match_caller_number=False)
    assert await lenient.find(CALL) == "hidden"


async def test_the_lookup_reader_waits_for_the_platform_then_reads_under_its_id() -> None:
    class Late:
        def __init__(self) -> None:
            self.calls = 0

        async def find(self, call: PhoneCall) -> str:
            self.calls += 1
            if self.calls < 3:
                raise TraceNotFound("trace not found: not listed yet")
            return "conv-9"

    late, inner = Late(), StoredDate()
    reader = LookupTraceReader(late, inner, lambda ref: CALL, wait=no_wait)
    trace = await reader.read("CA1")
    assert trace.call_ref == "conv-9" and late.calls == 3
    await reader.read("CA1")
    assert late.calls == 3, "a call already found is not searched for again"

    unknown = LookupTraceReader(late, inner, lambda ref: None, wait=no_wait)
    with pytest.raises(TraceNotFound, match="nothing is known"):
        await unknown.read("CA2")

    never = LookupTraceReader(FinderByOrder(finds=False), inner, lambda ref: CALL, wait=no_wait)
    with pytest.raises(TraceNotFound):
        await never.read("CA1")


# ----- the list of numbers that may be dialled -----------------------------------


def test_only_listed_numbers_are_allowed_and_a_near_match_is_not(tmp_path: Path) -> None:
    listing = tmp_path / "allowed.txt"
    listing.write_text("# test agent\n+44 1111 111111  # the test line\n\n+445555555555\n")
    allowed = allowed_numbers(str(listing), {ALLOWED_ENV: "+446666666666, "})
    assert allowed == ("+44 1111 111111", "+445555555555", "+446666666666")
    assert is_allowed(TO, allowed)
    assert not is_allowed("+441111111112", allowed)
    assert not is_allowed("01111111111", allowed), "permission to dial is exact, not a suffix"
    assert not is_allowed("", allowed) and not is_allowed(TO, ())
    assert allowed_numbers("+441, +442", {}) == ("+441", "+442")
    assert allowed_numbers("", {}) == ()
    # Matching a trace is looser than permission to dial: national and full forms agree.
    assert same_number("07400 123456", "+44 7400 123456")
    assert not same_number("+447400123456", "+447400123457") and not same_number("", "")


async def test_a_number_not_on_the_list_is_never_sent_to_the_carrier() -> None:
    fake = FakeTwilio(agent_lines=[0.2])
    leg = TwilioMediaLeg(
        PhoneConfig(
            to_number="+449999999999",
            from_number=FROM,
            public_url="http://127.0.0.1:1",
            listen_host="127.0.0.1",
            listen_port=0,
            allowed_to=(TO,),
        ),
        fake,
        auth_token=TOKEN,
    )
    with pytest.raises(NumberNotAllowed, match="not on the list"):
        await leg.connect()
    assert fake.created == [], "the refusal must come before the carrier is asked for anything"


def batch_arguments(tmp_path: Path, bank: ClipBank, *extra: str) -> list[str]:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir(exist_ok=True)
    return [
        "batch",
        str(scenarios),
        "--leg",
        "phone",
        "--clips",
        str(bank.directory),
        "--out",
        str(tmp_path / "runs"),
        "--public-url",
        "https://caller.example.com",
        "--from",
        FROM,
        "--to",
        TO,
        *extra,
    ]


def test_batch_refuses_to_dial_without_a_list_or_off_it(
    bank: ClipBank, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOWED_ENV, raising=False)
    with pytest.raises(SystemExit, match="no numbers are allowed"):
        cli.main(batch_arguments(tmp_path, bank))
    with pytest.raises(SystemExit, match="not on the list"):
        cli.main(batch_arguments(tmp_path, bank, "--allowed-numbers", "+445555555555"))
    with pytest.raises(SystemExit, match="one at a time"):
        cli.main(batch_arguments(tmp_path, bank, "--allowed-numbers", TO, "--max-parallel", "2"))
    assert not (tmp_path / "runs").exists(), "nothing may be written when nothing ran"


def test_the_single_call_command_also_needs_the_number_listed(
    bank: ClipBank, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"name": "x", "steps": [{"when": "help", "clips": ["opener.wav"]}]}))
    arguments = [
        "--scenario",
        str(path),
        "--clips",
        str(bank.directory),
        "--out",
        str(tmp_path / "r"),
    ]
    arguments += ["--from", FROM, "--to", TO, "--public-url", "https://caller.example.com"]
    with pytest.raises(SystemExit) as stopped:
        phone_main(arguments, env={"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t"})
    assert stopped.value.code == 2
    assert "not on the list of numbers" in capsys.readouterr().err
    assert not (tmp_path / "r").exists()

    # Listed, but the scenario does not say what it changes: the batch's guard applies here too.
    with pytest.raises(SystemExit) as guarded:
        phone_main(
            [*arguments, "--allowed-numbers", TO],
            env={"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t"},
        )
    assert guarded.value.code == 2
    assert "mutation guard" in capsys.readouterr().err
    assert not (tmp_path / "r").exists()


# ----- the exchange -------------------------------------------------------------


async def test_the_exchange_gives_nothing_to_a_path_no_call_registered() -> None:
    exchange = PhoneExchange("127.0.0.1", 0)
    port = await exchange.start()
    try:
        async with httpx.AsyncClient() as client:
            status = await client.get(f"http://127.0.0.1:{port}/status/not-a-token?CallStatus=x")
            probe = await client.get(f"http://127.0.0.1:{port}/probe/not-the-probe")
        assert (status.status_code, probe.status_code) == (404, 404)
        with pytest.raises(websockets.ConnectionClosed) as closed:
            async with websockets.connect(f"ws://127.0.0.1:{port}/media/not-a-token") as stream:
                await stream.recv()
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401
        assert len(exchange.rejected) == 2
    finally:
        await exchange.close()


# ----- the recorder -------------------------------------------------------------


def test_the_recorder_places_audio_by_time_and_joins_what_jitter_split(tmp_path: Path) -> None:
    recorder = CallRecorder(RATE)
    frame = tone_pcm(0.02)
    recorder.put("agent", 0.0, frame)
    recorder.put("agent", 0.021, frame)  # a millisecond late: joined, no hole
    assert recorder.seconds("agent") == pytest.approx(0.04)
    recorder.put("caller", 1.0, frame)  # a real silence before it: kept
    assert recorder.seconds("caller") == pytest.approx(1.02)
    saved = recorder.save(tmp_path, "CA1")
    assert saved["seconds"] == pytest.approx(1.02) and saved["rate"] == RATE
    caller, agent = pcm_of(tmp_path / saved["caller"]), pcm_of(tmp_path / saved["agent"])
    assert len(caller) == len(agent)
    assert rms(caller[: RATE * 2]) == 0 and rms(caller[RATE * 2 :]) > 250
    assert CallRecorder(RATE).empty


# ----- preflight ----------------------------------------------------------------

ENV = {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "DEEPGRAM_API_KEY": "d"}
ENV = {**ENV, "ELEVENLABS_API_KEY": "e"}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def twilio(numbers: list[dict[str, Any]], seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"incoming_phone_numbers": numbers})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_preflight_passes_a_ready_line_and_places_no_call() -> None:
    port = free_port()
    seen: list[httpx.Request] = []
    checks = await phone_preflight(
        env=ENV,
        to_number=TO,
        from_number=FROM,
        public_url=f"http://127.0.0.1:{port}",
        allowed_argument=TO,
        host="127.0.0.1",
        port=port,
        twilio_client=twilio([{"phone_number": FROM, "voice_url": "https://demo.example/x"}], seen),
    )
    text, code = render_preflight(checks)
    assert code == 0, text
    assert "ready for a first call" in text
    by_name = {check.name: check for check in checks}
    assert by_name["public address reaches this caller"].ok
    assert by_name["public address carries a media stream"].ok
    assert by_name["caller number is on the account"].ok
    # The number's own webhook points elsewhere: said, but outbound calls do not use it.
    webhook = by_name["caller number's own webhook points at this caller"]
    assert not webhook.ok and not webhook.fatal and "[warn]" in text
    # The carrier was only read from: one GET for the number, and nothing that dials.
    assert [request.method for request in seen] == ["GET"]
    assert seen[0].url.path.endswith("/IncomingPhoneNumbers.json")
    assert seen[0].url.params["PhoneNumber"] == FROM


async def test_preflight_names_each_thing_that_is_not_ready() -> None:
    port = free_port()
    checks = await phone_preflight(
        env={"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t"},
        to_number="+449999999999",
        from_number="02222222222",
        # An address nothing listens on: the probe must fail, not hang or pass.
        public_url=f"http://127.0.0.1:{free_port()}",
        allowed_argument=TO,
        host="127.0.0.1",
        port=port,
        twilio_client=twilio([], []),
    )
    text, code = render_preflight(checks)
    assert code == 1
    failed = {check.name for check in checks if not check.ok and check.fatal}
    assert failed == {
        "transcriber key",
        "trace lookup key",
        "caller number is written in full",
        "number to dial is allowed",
        "caller number is on the account",
        "public address reaches this caller",
        "public address carries a media stream",
    }
    assert "not ready: 7 check(s) failed" in text and "AC1" not in text

    missing = await phone_preflight(
        env={},
        to_number=TO,
        from_number=FROM,
        public_url=f"http://127.0.0.1:{free_port()}",
        allowed_argument="",
        host="127.0.0.1",
        port=free_port(),
        transcriber="none",
        trace=False,
    )
    names = {check.name: check.ok for check in missing}
    assert names["carrier credentials"] is False
    assert names["number to dial is allowed"] is False
    assert "caller number is on the account" not in names, "no credentials, so nothing is read"
