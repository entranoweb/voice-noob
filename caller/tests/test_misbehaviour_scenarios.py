"""Audit item 16: one shipped scenario per caller misbehaviour, each gradeable.

``talk_over``, ``stay_silent``, ``delay_s`` and keypad digits (``dtmf``) each have a
scenario in ``scenarios/example-receptionist``. Here every one runs on the fake leg
against a scripted good agent (it passes) and a scripted bad agent (it fails, for the
reason the scenario exists). Nothing here says anything about the live agent: running
them against it, on the phone leg for the keypad one, is the owner's job.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from synthiq_caller import ci_gate
from synthiq_caller.cli import main
from synthiq_caller.scenario import load_scenario

CALLER = Path(__file__).parent.parent
SCENARIOS = CALLER / "scenarios" / "example-receptionist"
AGENTS = CALLER / "ci" / "agents" / "misbehaviour"

# scenario file -> (the behaviour it exercises, the bad agent that must fail it, the failed check)
CASES: dict[str, tuple[str, str, str]] = {
    "21-talk-over-the-date-of-birth": (
        "talk_over",
        "loses-the-talked-over-answer",
        "example_v2_verify_identity' is called and succeeds",
    ),
    "22-silence-then-date-of-birth": (
        "stay_silent",
        "gives-up-on-silence",
        "example_v2_verify_identity' is called and succeeds",
    ),
    "23-slow-answer-date-of-birth": (
        "delay_s",
        "hangs-up-on-a-slow-answer",
        "example_v2_verify_identity' is called and succeeds",
    ),
    "24-keypad-date-of-birth": (
        "dtmf",
        "ignores-the-keypad",
        "example_v2_verify_identity' is called and succeeds",
    ),
}


def run(tmp_path: Path, stem: str, agent: Path) -> dict[str, Any]:
    """One scenario on the fake leg against one scripted agent; the stored run."""
    scenarios, bank, out = tmp_path / "set", tmp_path / "bank", tmp_path / "runs"
    scenarios.mkdir()
    shutil.copy(SCENARIOS / f"{stem}.json", scenarios)
    ci_gate.make_bank(scenarios, bank)
    # The tools that write, as the target profile says (a lookup is not one of them).
    profile = tmp_path / "target.json"
    profile.write_text(
        json.dumps({"name": "rehearsal", "write_tools": ["_book", "reschedule", "register"]})
    )
    main(
        [
            "batch",
            str(scenarios),
            "--leg",
            "fake",
            "--fake-agent",
            str(agent),
            "--clips",
            str(bank),
            "--target",
            str(profile),
            "--allow-mutating",
            "none",
            "--out",
            str(out),
            "--settle",
            "0",
        ]
    )
    (path,) = sorted(out.glob("run-*.json"))
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_four_behaviours_each_have_a_scenario() -> None:
    assert {behaviour for behaviour, _, _ in CASES.values()} == {
        "talk_over",
        "stay_silent",
        "delay_s",
        "dtmf",
    }
    assert {p.stem for p in SCENARIOS.glob("2[1-4]-*.json")} == set(CASES)


@pytest.mark.parametrize("stem", sorted(CASES))
def test_each_scenario_uses_its_behaviour_and_states_expectations(stem: str) -> None:
    behaviour = CASES[stem][0]
    scenario = load_scenario(SCENARIOS / f"{stem}.json")
    used = {
        "talk_over": any(s.talk_over for s in scenario.steps),
        "stay_silent": any(s.stay_silent for s in scenario.steps),
        "delay_s": any(s.delay_s > 0 for s in scenario.steps),
        "dtmf": any(s.dtmf for s in scenario.steps),
    }
    assert used[behaviour]
    expect = scenario.expect
    assert expect.tools and (expect.agent_says or expect.agent_never_says or expect.fields)
    assert scenario.mutates == "none"
    assert not scenario.missing_clips


@pytest.mark.parametrize("stem", sorted(CASES))
def test_validate_strict_accepts_the_scenario(
    stem: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    only = tmp_path / "only"
    only.mkdir()
    shutil.copy(SCENARIOS / f"{stem}.json", only)
    assert main(["validate", str(only), "--strict"]) == 0
    assert "UNGRADEABLE" not in capsys.readouterr().out


@pytest.mark.parametrize("stem", sorted(CASES))
def test_the_good_agent_passes(tmp_path: Path, stem: str) -> None:
    record = run(tmp_path, stem, AGENTS / "good" / f"{stem}.json")
    assert record["grade"]["outcome"] == "passed", [
        c for c in record["grade"]["checks"] if not c["passed"]
    ]
    said = record["record"]["said"]
    behaviour = CASES[stem][0]
    if behaviour == "talk_over":
        assert any(s["talk_over"] for s in said)
    elif behaviour == "dtmf":
        assert [s["dtmf"] for s in said if s["dtmf"]] == ["01011995"]
    elif behaviour == "stay_silent":
        assert any(not s["clips"] and not s["dtmf"] for s in said)


@pytest.mark.parametrize("stem", sorted(CASES))
def test_the_known_bad_agent_fails(tmp_path: Path, stem: str) -> None:
    _, bad, check = CASES[stem]
    record = run(tmp_path, stem, AGENTS / "bad" / f"{bad}.json")
    grade = record["grade"]
    assert grade["outcome"] == "failed"
    assert grade["fault"] == "agent"
    assert any(check in c["what"] for c in grade["checks"] if not c["passed"]), [
        c["what"] for c in grade["checks"] if not c["passed"]
    ]


def test_every_bad_agent_is_used() -> None:
    assert {p.stem for p in (AGENTS / "bad").glob("*.json")} == {b for _, b, _ in CASES.values()}
