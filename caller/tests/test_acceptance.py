"""The acceptance report: what was tested, what passed, and what that does not show."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_manifest import rehearse
from test_post_merge_review import record

from synthiq_caller import acceptance, cli, manifest
from synthiq_caller.jsonio import write_json

COMMIT = "0123456789abcdef0123456789abcdef01234567"
WHEN = "2026-11-25 09:00 UTC"


def run_directory(
    root: Path,
    outcomes: list[str],
    *,
    name: str = "runs",
    leg: str = "elevenlabs-socket",
    version: str = "build 7",
    scenario_text: str = "{}",
    with_manifest: bool = True,
    blocked: int = 0,
    extra: dict[int, dict[str, Any]] | None = None,
) -> Path:
    """A stored batch: one call per scenario, ``outcomes[n]`` for scenario ``n``."""
    out = root / name
    out.mkdir(parents=True)
    scenarios = root / f"{name}-set"
    scenarios.mkdir()
    files = [f"s{n:03d}.json" for n in range(len(outcomes))]
    for file in files:
        (scenarios / file).write_text(scenario_text, encoding="utf-8")
    if with_manifest:
        batch = manifest.open_batch(
            out,
            argv=["batch", "set"],
            scenarios=manifest.scenario_identity(scenarios, files),
            clips={
                "banks": [
                    {
                        "directory": "clips",
                        "manifest_sha256": "a" * 64,
                        "overrides_sha256": "",
                        "clips": 40,
                    }
                ],
                "played": 12,
                "played_sha256": "b" * 64,
                "unavailable": [],
            },
            target=manifest.target_identity(
                label="test target", version=version, agent="agent-under-test", leg=leg, trace="x"
            ),
            started="2026-11-25T08:00:00+00:00",
        )
        manifest.close_batch(out, batch, ended="2026-11-25T08:40:00+00:00")
        stored = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        stored["entries"][0]["rig"] = {"version": "0.1.0", "commit": COMMIT, "dirty": False}
        write_json(out / "manifest.json", stored)
    for index, outcome in enumerate(outcomes):
        failed = outcome == "failed"
        payload: dict[str, Any] = {
            "scenario_file": files[index],
            "record": record().to_json(),
            "trace": {"call_ref": "c1", "tool_calls": [], "step_path": ["S1", "S9"]},
            "grade": {
                "scenario": f"scenario {index:03d}",
                "outcome": outcome,
                "checks": [{"what": "the booking tool ran", "passed": False}] if failed else [],
                "fields": [],
                "fault": {"failed": "agent", "void": "rig"}.get(outcome, ""),
                "reason": "the rig had no answer to a question" if outcome == "void" else "",
            },
            "labels_unverified": [],
            "synthetic_clips": [],
            **((extra or {}).get(index, {})),
        }
        if with_manifest:
            payload["batch"] = "b001"
        write_json(out / f"run-{index + 1:03d}.json", payload)
    held = [
        {"scenario_file": f"held{n}.json", "scenario": "x", "repeat": 1, "reason": "name budget"}
        for n in range(blocked)
    ]
    write_json(out / "batch.json", {"out": str(out), "ran": len(outcomes), "blocked": held})
    return out


def page(runs: Path, *options: str) -> tuple[int, str]:
    out = runs.parent / "report.html"
    code = cli.main(["acceptance", str(runs), "--claim", "0.95", "--out", str(out), *options])
    return code, out.read_text(encoding="utf-8") if out.exists() else ""


def test_a_run_that_meets_the_gate_says_so_with_what_produced_it(tmp_path: Path) -> None:
    code, text = page(run_directory(tmp_path, ["passed"] * 59))

    assert code == 0
    assert "The claim is met" in text
    assert "59 of 59 scenarios passed every call placed, and 59 are needed with 0 failing" in text
    # What was tested: the target, the rig, the scenarios, the clips.
    assert "test target" in text and "build 7" in text and "agent-under-test" in text
    assert COMMIT in text and "version 0.1.0" in text
    found = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert len(found["made_with"]["scenario_set_sha256"]) == 64
    assert found["made_with"]["scenario_set_sha256"] in text
    assert "clips: 40 clip(s)" in text
    # Counts beside the percentage, pooled and by scenario.
    assert "59 of 59 (100.0%; 95% interval 93.9% to 100.0%)" in text
    # And what it is not: this was not a phone call.
    assert "direct connection to the agent (leg: elevenlabs-socket), not a phone line" in text
    assert "as stated by whoever ran the batch" in text


def test_a_run_that_fails_the_gate_says_so_and_groups_its_failures(tmp_path: Path) -> None:
    code, text = page(run_directory(tmp_path, ["passed"] * 49 + ["failed"] * 10))

    assert code == 1
    assert "The claim is not met" in text and "The claim is met" not in text
    assert "49 of 59 scenarios passed every call placed (83.1%)" in text
    found = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert found["counts"]["failed_by_fault"] == {"agent": 10}
    (group,) = found["failure_groups"]
    assert (group["fault"], group["last_step"], group["count"]) == ("agent", "S9", 10)
    assert "the booking tool ran" in text and "<code>S9</code>" in text
    assert "10 charged to agent" in text


def test_too_few_calls_is_never_a_pass(tmp_path: Path) -> None:
    code, text = page(run_directory(tmp_path, ["passed"] * 12))

    assert code == 1
    assert "Not enough calls to support the claim" in text
    assert "The claim is met" not in text
    assert "59 scenarios are needed to demonstrate 95.0%; 12 were tested" in text
    # Every call passed, and the page still does not say the agent passed.
    assert "12 of 12 (100.0%; 95% interval 75.7% to 100.0%)" in text
    found = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert found["verdict"] == acceptance.NOT_ENOUGH and found["gate"]["met"] is False


def test_one_failure_in_ninety_three_meets_it_and_in_ninety_two_does_not(tmp_path: Path) -> None:
    met = acceptance.build([run_directory(tmp_path / "a", ["passed"] * 92 + ["failed"])])
    short = acceptance.build([run_directory(tmp_path / "b", ["passed"] * 91 + ["failed"])])

    assert met["verdict"] == acceptance.MET and met["gate"]["needs"] == [59, 93, 124]
    # 91 of 92 is 98.9%: above the claim, and one result short of demonstrating it.
    assert short["verdict"] == acceptance.NOT_ENOUGH
    assert "93 scenarios are needed" in short["verdict_reason"]


def test_unverified_labels_and_synthetic_clips_are_on_the_page(tmp_path: Path) -> None:
    extra = {
        0: {"labels_unverified": ["dob_03: flagged low confidence, not checked by a person"]},
        1: {"synthetic_clips": ["no_preference_short"]},
    }
    code, text = page(run_directory(tmp_path, ["passed"] * 59, extra=extra))

    assert code == 0
    assert "Labels not all confirmed." in text and "<code>dob_03</code>" in text
    assert "flagged low confidence, not checked by a person" in text
    assert "Synthetic voice." in text and "<code>no_preference_short</code>" in text
    assert "1 call(s) played a synthetic voice where no recording of a real caller exists" in text
    assert "1 call(s) played a clip whose label no person has confirmed" in text

    _, clean = page(run_directory(tmp_path / "clean", ["passed"] * 59))
    assert "Every clip played carried a label a person has confirmed" in clean
    assert "No synthetic voice was played" in clean


def test_the_same_runs_always_make_the_same_page(tmp_path: Path) -> None:
    runs = run_directory(tmp_path, ["passed"] * 30 + ["failed", "void"], blocked=1)
    first = acceptance.render_html(acceptance.build([runs], generated=WHEN))
    again = acceptance.render_html(acceptance.build([runs], generated=WHEN))
    later = acceptance.render_html(acceptance.build([runs], generated="2026-12-01 10:00 UTC"))

    assert first == again
    differing = [
        (a, b) for a, b in zip(first.splitlines(), later.splitlines(), strict=True) if a != b
    ]
    assert len(differing) == 1 and "Generated" in differing[0][0]
    # Nothing of the machine it was made on.
    assert str(tmp_path) not in first


def test_every_call_left_out_is_listed_with_its_reason(tmp_path: Path) -> None:
    runs = run_directory(tmp_path, ["passed"] * 57 + ["void", "ungraded"], blocked=2)
    found = acceptance.build([runs], generated=WHEN)
    text = acceptance.render_html(found)

    assert found["counts"] == {
        "attempted": 61,
        "placed": 59,
        "not_run": 2,
        "graded": 57,
        "passed": 57,
        "failed": 0,
        "failed_by_fault": {},
        "void": 1,
        "ungraded": 1,
        "ungradeable": 0,
    }
    assert [(n["run"], n["fault"]) for n in found["excluded"]] == [
        ("runs/run-058.json", "rig"),
        ("runs/run-059.json", "scenario"),
    ]
    assert "void: the rig had no answer to a question" in text
    assert "name budget" in text and "held0.json" in text
    # Three readings of the rate, each with its counts.
    assert "57 of 57 (100.0%" in text and "57 of 59 (96.6%" in text and "57 of 61 (93.4%" in text
    # The ungraded call counts against its scenario, so the claim is not met.
    assert found["verdict"] == acceptance.NOT_ENOUGH
    assert found["gate"]["every"] == 57 and found["gate"]["scenarios"] == 59


def test_too_much_left_unjudged_refuses_the_gate_whatever_the_numbers(tmp_path: Path) -> None:
    found = acceptance.build([run_directory(tmp_path, ["passed"] * 59, blocked=30)])

    assert found["verdict"] == acceptance.NOT_MET
    assert "was not judged" in found["verdict_reason"]
    assert "The gate is refused" in acceptance.render_html(found)


def test_each_disagreement_is_shown_with_where_stored_came_from(tmp_path: Path) -> None:
    row = {
        "field": "date_of_birth",
        "said": "1994-11-05",
        "heard": "15 November 1994",
        "stored": "1994-11-15",
        "heard_matches": False,
        "stored_matches": False,
        "stored_source": "tool call",
        "verdict": "misheard",
    }
    runs = run_directory(tmp_path, ["passed"] * 58 + ["failed"])
    last = runs / "run-059.json"
    payload = json.loads(last.read_text(encoding="utf-8"))
    payload["grade"]["fields"] = [row]
    write_json(last, payload)
    found = acceptance.build([runs], generated=WHEN)
    text = acceptance.render_html(found)

    (field,) = found["fields"]
    assert field["disagreements"][0]["stored_source"] == "tool call"
    assert "1994-11-05" in text and "15 November 1994" in text and "tool call" in text
    assert "Stored taken from" in text
    assert any("not what the system was afterwards found to hold" in s for s in found["not_shown"])


def test_a_rehearsal_is_refused_and_no_page_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, text = page(rehearse(tmp_path))

    assert code == 2 and text == ""
    assert "rehearsal against a scripted stand-in" in capsys.readouterr().out
    assert not (tmp_path / "report.html").exists()


def test_runs_against_two_versions_of_a_scenario_are_refused_unless_told_to(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monday = run_directory(tmp_path / "a", ["passed"] * 30, name="monday")
    tuesday = run_directory(
        tmp_path / "b", ["passed"] * 30, name="tuesday", scenario_text='{"changed": true}'
    )
    out = tmp_path / "report.html"
    command = ["acceptance", str(monday), str(tuesday), "--out", str(out)]

    assert cli.main(command) == 2 and not out.exists()
    assert "different versions of the same scenario file" in capsys.readouterr().out

    assert cli.main([*command, "--allow-mixed-scenarios"]) == 1
    text = out.read_text(encoding="utf-8")
    assert "no single recorded version" in text
    assert "Some scenario files were changed between runs" in text

    # The same scenarios on both days are one test.
    wednesday = run_directory(tmp_path / "c", ["passed"] * 30, name="wednesday")
    assert acceptance.build([monday, wednesday])["made_with"]["mixed"] == {}


def test_runs_with_no_manifest_are_refused_unless_told_to(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = run_directory(tmp_path, ["passed"] * 59, with_manifest=False)

    code, text = page(runs)
    assert code == 2 and text == ""
    assert "no run manifest" in capsys.readouterr().out

    code, text = page(runs, "--allow-no-manifest")
    assert code == 0
    assert "not recorded" in text
    assert "cannot be tied to a rig version, a scenario version or a clip bank" in text
    assert "nothing here can be taken as a test over a phone line" in text


def test_an_empty_directory_makes_no_report(tmp_path: Path) -> None:
    (tmp_path / "runs").mkdir()
    with pytest.raises(acceptance.Refused, match="no runs found"):
        acceptance.build([tmp_path / "runs"])


def test_a_percentage_is_never_shown_on_the_wrong_side_of_the_claim() -> None:
    assert acceptance.pct(0.9496, 0.95) == "94.96%"
    assert acceptance.pct(0.95, 0.95) == "95.0%"
    assert acceptance.pct(0.95004, 0.95) == "95.004%"
    assert acceptance.pct(0.57, 0.57) == "57.0%"
    assert acceptance.pct(0.983) == "98.3%"


def test_a_phone_leg_drops_the_direct_connection_caveat(tmp_path: Path) -> None:
    found = acceptance.build([run_directory(tmp_path, ["passed"] * 59, leg="phone", version="")])

    assert not any("direct connection" in limit for limit in found["not_shown"])
    assert any("over a phone line (leg: phone)" in limit for limit in found["not_shown"])
    assert any("was not recorded for every batch" in limit for limit in found["not_shown"])


# --- an ungradeable scenario behind the runs: no report ---------------------------


def test_acceptance_refuses_runs_made_from_a_scenario_that_states_no_expectations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = run_directory(
        tmp_path,
        ["passed"] * 58 + ["ungraded"],
        extra={58: {"states_expectations": False}},
    )
    with pytest.raises(acceptance.Refused, match=r"s058\.json.*states no expectations"):
        acceptance.build([runs])
    code, text = page(runs)
    assert code == 2 and text == ""
    assert "states no expectations" in capsys.readouterr().out
    assert not (tmp_path / "report.json").exists()


def test_acceptance_refuses_when_the_batch_held_back_an_ungradeable_scenario(
    tmp_path: Path,
) -> None:
    runs = run_directory(tmp_path, ["passed"] * 59)
    write_json(
        runs / "batch.json",
        {
            "out": str(runs),
            "ran": 59,
            "blocked": [
                {
                    "scenario_file": "bare.json",
                    "scenario": "x",
                    "repeat": 1,
                    "reason": "UNGRADEABLE: the scenario states no expectations, so no call "
                    "could pass it (state them, or allow ungradeable scenarios for this batch)",
                }
            ],
        },
    )
    with pytest.raises(acceptance.Refused, match=r"bare\.json"):
        acceptance.build([runs])


def test_acceptance_accepts_runs_whose_scenarios_state_expectations(tmp_path: Path) -> None:
    runs = run_directory(
        tmp_path, ["passed"] * 59, extra={n: {"states_expectations": True} for n in range(59)}
    )
    assert acceptance.build([runs])["verdict"] == acceptance.MET
