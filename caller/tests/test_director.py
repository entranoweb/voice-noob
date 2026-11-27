"""The director against a scripted agent. Real time, kept short."""

import wave
from pathlib import Path

import pytest

from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.scenario import ScenarioError, parse_scenario
from synthiq_caller.turns import TurnSettings

FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)


def write_clip(directory: Path, name: str, seconds: float, rate: int = RATE) -> None:
    with wave.open(str(directory / name), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(tone(seconds, rate))


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    for name, seconds in (
        ("opener.wav", 0.3),
        ("first.wav", 0.2),
        ("last.wav", 0.2),
        ("dob.wav", 0.3),
    ):
        write_clip(tmp_path, name, seconds)
    (tmp_path / "manifest.csv").write_text(
        "clip,call,category,start,end,secs,said,question,checked_by_human\n"
        "dob,call-1,dob,0,0.3,0.3,5th July 1987,date of birth?,\n",
        encoding="utf-8",
    )
    return ClipBank(tmp_path, allow_unknown_provenance=True, allow_missing_consent=True)


def overlaps(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    return any(s1 < e2 and s2 < e1 for s1, e1 in a for s2, e2 in b)


async def test_plays_the_right_clips_and_never_speaks_over_the_agent(bank: ClipBank) -> None:
    scenario = parse_scenario(
        {
            "name": "registration to date of birth",
            "persona": {"facts": {"date_of_birth": "1987-07-05"}},
            "steps": [
                {"when": "how can i help", "clips": ["opener.wav"], "intended": "book please"},
                {
                    "when": "full name",
                    "clips": ["first.wav", "last.wav"],
                    "gap_s": 0.1,
                    "field": "name",
                },
                {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
            ],
            "hangup_after": "mobile number",
            "max_seconds": 20,
        }
    )
    leg = FakeLeg(
        [
            AgentLine("Hi, how can I help you today?", speech_s=0.5),
            AgentLine(
                "Could I take your full name, please?", speech_s=0.4, hears="It's Elliot Allen."
            ),
            AgentLine("I'm sorry to hear that.", speech_s=0.3, patience_s=0.4),
            AgentLine("Could I take your date of birth?", speech_s=0.4, hears="6th July, 1987."),
            AgentLine("And the best mobile number?", speech_s=0.4),
        ]
    )
    record = await Director(scenario, leg, bank, turn_settings=FAST, chunk_ms=20).run()

    assert [s.clips for s in record.said] == [
        ["opener.wav"],
        ["first.wav", "last.wav"],
        ["dob.wav"],
    ]
    assert record.ended == "caller hung up: scenario end"
    # The caller's speech never lands on top of the agent's.
    assert leg.caller_speech and not overlaps(leg.caller_speech, leg.agent_speech)
    # What the recogniser reported is attached to the answer it followed.
    assert record.said[2].field == "date_of_birth"
    assert record.said[2].heard == ["6th July, 1987."]
    # A line that asks nothing is noted quietly, not answered.
    kinds = [e["kind"] for e in record.timeline]
    assert "no_answer_needed" in kinds
    assert record.call_ref == "fake-call"


async def test_two_clips_are_joined_into_one_utterance(bank: ClipBank) -> None:
    pcm = bank.joined(["first.wav", "last.wav"], RATE, gap_s=0.1)
    assert len(pcm) == int((0.2 + 0.1 + 0.2) * RATE) * 2


async def test_clip_at_another_rate_is_only_resampled_when_needed(
    bank: ClipBank, tmp_path: Path
) -> None:
    # Same rate: read straight from the file, no external tool involved.
    assert len(bank.pcm("dob.wav", RATE)) == int(0.3 * RATE) * 2
    assert bank.info("dob.wav") is not None and bank.info("dob.wav").said == "5th July 1987"  # type: ignore[union-attr]
    with pytest.raises(FileNotFoundError):
        bank.pcm("missing.wav", RATE)


async def test_talk_over_speaks_while_the_agent_is_still_talking(bank: ClipBank) -> None:
    scenario = parse_scenario(
        {
            "name": "talk over",
            "steps": [{"when": "how can i help", "clips": ["opener.wav"], "talk_over": True}],
            "hangup_after": "never matches this",
            "max_seconds": 2.5,
        }
    )
    leg = FakeLeg([AgentLine("Hi, how can I help you today?", speech_s=1.2, patience_s=0.6)])
    record = await Director(scenario, leg, bank, turn_settings=FAST, chunk_ms=20).run()
    assert record.said and record.said[0].talk_over
    assert overlaps(leg.caller_speech, leg.agent_speech)


async def test_delay_and_silence_are_honoured(bank: ClipBank) -> None:
    scenario = parse_scenario(
        {
            "name": "late then silent",
            "steps": [
                {"when": "how can i help", "clips": ["opener.wav"], "delay_s": 0.5},
                {"when": "full name", "stay_silent": True},
            ],
            "max_seconds": 4,
        }
    )
    leg = FakeLeg(
        [
            AgentLine("How can I help?", speech_s=0.3, patience_s=2.0),
            AgentLine("Your full name?", speech_s=0.3, patience_s=0.5),
        ]
    )
    record = await Director(scenario, leg, bank, turn_settings=FAST, chunk_ms=20).run()
    agent_end = leg.agent_speech[0][1]
    first_caller = leg.caller_speech[0][0]
    assert first_caller - agent_end >= 0.5 + FAST.hangover_s - 0.1
    assert [s.clips for s in record.said] == [["opener.wav"], []]
    # After the silent step the caller sent no more speech.
    assert all(start < leg.agent_speech[1][0] for start, _ in leg.caller_speech)
    assert "agent ended the call" in record.ended


async def test_keypad_digits_go_to_the_leg(bank: ClipBank) -> None:
    scenario = parse_scenario(
        {
            "name": "keypad",
            "steps": [{"when": "key in", "dtmf": "05071987", "field": "date_of_birth"}],
            "persona": {"facts": {"date_of_birth": "1987-07-05"}},
            "max_seconds": 3,
        }
    )
    leg = FakeLeg([AgentLine("Please key in your date of birth.", speech_s=0.3, patience_s=1.0)])
    record = await Director(scenario, leg, bank, turn_settings=FAST, chunk_ms=20).run()
    assert leg.dtmf == ["05071987"]
    assert record.said[0].dtmf == "05071987"


def test_scenarios_are_validated() -> None:
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "steps": []})
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "steps": [{"when": "a"}]})
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "steps": [{"when": "(", "clips": ["a.wav"]}]})
    with pytest.raises(ScenarioError):
        parse_scenario(
            {
                "name": "x",
                "steps": [{"when": "a", "clips": ["a.wav"]}],
                "expect": {"fields": ["postcode"]},
            }
        )
    with pytest.raises(ScenarioError):
        parse_scenario(
            {"name": "x", "steps": [{"when": "a", "clips": ["a.wav"], "field": "shoe_size"}]}
        )


async def test_resampling_runs_off_the_event_loop_thread(
    bank: ClipBank, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F19: the phone leg's ffmpeg resample must not block the loop that pumps audio."""
    import threading

    from synthiq_caller import clips as clips_module

    write_clip(tmp_path, "opener.wav", 0.2, rate=RATE * 2)  # another rate: needs a resample
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def fake_resample(path: Path, rate: int) -> bytes:
        seen.append(threading.get_ident())
        return tone(0.2, rate)

    monkeypatch.setattr(clips_module, "resample_with_ffmpeg", fake_resample)
    scenario = parse_scenario(
        {
            "name": "resample",
            "persona": {"facts": {}},
            "steps": [{"when": "how can i help", "clips": ["opener.wav"]}],
            "hangup_after": "anything else",
            "max_seconds": 20,
        }
    )
    leg = FakeLeg(
        [
            AgentLine("Hi, how can I help you today?", speech_s=0.3),
            AgentLine("Is there anything else?", speech_s=0.3),
        ]
    )
    await Director(scenario, leg, bank, turn_settings=FAST, chunk_ms=20).run()
    assert seen, "the clip was never resampled"
    assert loop_thread not in seen
