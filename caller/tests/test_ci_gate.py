"""The scheduled run's gate: the allow-list, the mutation refusal, and the baseline compare."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from synthiq_caller import ci_gate
from synthiq_caller.batch import load_chains
from synthiq_caller.ci_gate import GateRefused
from synthiq_caller.cli import main
from synthiq_caller.scenario import ScenarioError

CALLER = Path(__file__).parent.parent
SETS = CALLER / "ci" / "scenarios"
AGENTS = CALLER / "ci" / "agents"
BASELINE = CALLER / "baselines" / "smoke.json"
WORKFLOW = CALLER.parent / ".github" / "workflows" / "caller-scheduled.yml"


def write_scenario(folder: Path, name: str, mutates: str | None, expectations: bool = True) -> None:
    data: dict[str, Any] = {
        "name": name,
        "persona": {"facts": {"name": "Alice Wonderland"}},
        "steps": [{"when": "how can i help", "clips": ["ci-opener"]}],
        "hangup_after": "mobile number",
        "max_seconds": 20,
        "expect": {"tools": [{"name_contains": "lookup"}] if expectations else [], "fields": []},
    }
    if mutates is not None:
        data["mutates"] = mutates
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


# --- the allow-list ---------------------------------------------------------------


def test_a_target_on_the_list_passes() -> None:
    ci_gate.check_target("rehearsal", "other, rehearsal")
    ci_gate.check_target("rehearsal", "other\nrehearsal\n")


def test_a_target_not_on_the_list_is_refused() -> None:
    with pytest.raises(GateRefused, match="not on the allow-list"):
        ci_gate.check_target("elsewhere", "rehearsal,other")


def test_an_empty_list_allows_nothing() -> None:
    for raw in ("", "  ", ",,\n,", None):
        with pytest.raises(GateRefused, match="allow-list is empty"):
            ci_gate.check_target("rehearsal", raw)


def test_the_match_is_exact_and_has_no_wildcard() -> None:
    for raw in ("*", "rehearsal-2", "REHEARSAL", "rehears"):
        with pytest.raises(GateRefused):
            ci_gate.check_target("rehearsal", raw)


def test_no_target_is_refused_even_with_a_list() -> None:
    with pytest.raises(GateRefused, match="no target"):
        ci_gate.check_target("  ", "rehearsal")


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden", "a..b", "x y", "-x"])
def test_a_set_name_cannot_leave_its_folder(name: str) -> None:
    with pytest.raises(GateRefused, match="not allowed"):
        ci_gate.safe_set_name(name)


# --- the mutation refusal ---------------------------------------------------------


def test_a_read_only_set_passes(tmp_path: Path) -> None:
    write_scenario(tmp_path, "a", "none")
    assert ci_gate.check_scenarios(tmp_path) == 1


@pytest.mark.parametrize("level", ["registers", "books", "moves"])
def test_a_scenario_that_changes_something_is_refused(tmp_path: Path, level: str) -> None:
    write_scenario(tmp_path, "a", "none")
    write_scenario(tmp_path, "b", level)
    with pytest.raises(GateRefused, match=rf"b\.json \(mutates: {level}\)"):
        ci_gate.check_scenarios(tmp_path)


def test_a_scenario_that_does_not_say_what_it_changes_is_refused(tmp_path: Path) -> None:
    write_scenario(tmp_path, "a", None)
    with pytest.raises(GateRefused, match="unknown"):
        ci_gate.check_scenarios(tmp_path)


def test_a_missing_or_empty_set_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GateRefused, match="does not exist"):
        ci_gate.check_scenarios(tmp_path / "nope")
    with pytest.raises(GateRefused, match="no scenarios"):
        ci_gate.check_scenarios(tmp_path)


def test_a_set_that_cannot_be_read_is_refused_not_skipped(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(GateRefused, match="cannot be loaded"):
        ci_gate.check_scenarios(tmp_path)


def test_the_command_exits_one_with_a_reason_and_zero_when_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_scenario(tmp_path / "set", "a", "books")
    write_scenario(tmp_path / "ok", "a", "none")

    def run(target: str, allowed: str, name: str) -> int:
        argv = ["check", "--target", target, "--allowed", allowed]
        return ci_gate.main([*argv, "--root", str(tmp_path), "--set", name])

    assert run("t", "t", "set") == 1
    assert "mutating scenarios are refused" in capsys.readouterr().err
    assert run("t", "t", "ok") == 0
    assert run("other", "t", "ok") == 1
    assert "not on the allow-list" in capsys.readouterr().err
    assert run("t", "", "ok") == 1
    assert "allow-list is empty" in capsys.readouterr().err


def test_the_command_refuses_a_set_name_that_climbs_out(tmp_path: Path) -> None:
    args = ["check", "--target", "t", "--allowed", "t", "--root", str(tmp_path), "--set", "../x"]
    assert ci_gate.main(args) == 1


def test_the_batch_guard_it_relies_on_holds_a_mutating_scenario_back(tmp_path: Path) -> None:
    """The workflow also passes ``--allow-mutating none``: a booking scenario is not placed."""
    write_scenario(tmp_path / "set", "books", "books")
    bank = tmp_path / "bank"
    assert ci_gate.make_bank(tmp_path / "set", bank) == ["ci-opener"]
    out = tmp_path / "out"
    agent = AGENTS / "smoke"
    main(
        [
            "batch",
            str(tmp_path / "set"),
            "--leg",
            "fake",
            "--fake-agent",
            str(agent),
            "--clips",
            str(bank),
            "--allow-mutating",
            "none",
            "--out",
            str(out),
        ]
    )
    assert not list(out.glob("run-*.json"))
    assert "mutation guard" in (out / "batch.json").read_text(encoding="utf-8")


# --- the committed set, and the baseline compare ----------------------------------


def test_the_committed_set_is_read_only_and_allowed_for_a_listed_target() -> None:
    assert ci_gate.check("rehearsal", "rehearsal", SETS / "smoke") >= 1


def test_the_bank_is_generated_tones_marked_synthetic_and_holds_no_recording(
    tmp_path: Path,
) -> None:
    names = ci_gate.make_bank(SETS / "smoke", tmp_path)
    assert names
    manifest = (tmp_path / "manifest.csv").read_text(encoding="utf-8")
    assert manifest.count("synthetic: generated tone") == len(names)
    for name in names:
        assert (tmp_path / f"{name}.wav").stat().st_size < 20_000


def run_smoke(tmp_path: Path, agent: Path) -> Path:
    bank, out = tmp_path / "bank", tmp_path / "runs"
    ci_gate.make_bank(SETS / "smoke", bank)
    main(
        [
            "batch",
            str(SETS / "smoke"),
            "--leg",
            "fake",
            "--fake-agent",
            str(agent),
            "--clips",
            str(bank),
            "--allow-mutating",
            "none",
            "--out",
            str(out),
        ]
    )
    return out


def test_the_stored_baseline_is_met_by_the_committed_rehearsal(tmp_path: Path) -> None:
    out = run_smoke(tmp_path, AGENTS / "smoke")
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob("run-*.json"))]
    assert runs and all(run["rehearsal"] is True for run in runs)
    assert main(["compare", str(out), "--baseline", str(BASELINE), "--fail-on", "any"]) == 0


def test_a_regression_against_the_baseline_fails_the_compare(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same agent, but the tool the scenarios expect is never called.
    broken = tmp_path / "broken"
    broken.mkdir()
    script = json.loads((AGENTS / "smoke" / "default.json").read_text(encoding="utf-8"))
    script["trace"] = {"tool_calls": [], "step_path": []}
    (broken / "default.json").write_text(json.dumps(script), encoding="utf-8")
    out = run_smoke(tmp_path, broken)
    capsys.readouterr()
    code = main(["compare", str(out), "--baseline", str(BASELINE), "--fail-on", "any"])
    assert code == 1
    assert "regressions: 2" in capsys.readouterr().out


def test_a_baseline_from_an_older_grading_rule_is_refused(tmp_path: Path) -> None:
    old = tmp_path / "old.json"
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    old.write_text(json.dumps({**data, "version": 1}), encoding="utf-8")
    out = run_smoke(tmp_path, AGENTS / "smoke")
    with pytest.raises(ValueError, match="make the baseline again"):
        main(["compare", str(out), "--baseline", str(old), "--fail-on", "any"])


def test_the_committed_baseline_names_exactly_the_committed_scenarios() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    names = {
        json.loads(p.read_text(encoding="utf-8"))["name"] for p in (SETS / "smoke").glob("*.json")
    }
    assert set(baseline["scenarios"]) == names


# --- the workflow's own rules -----------------------------------------------------


def test_the_workflow_keeps_its_promises() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    runner = "${{ vars.USE_SELF_HOSTED == 'true' && fromJSON('[\"self-hosted\", \"linux\", \"x64\"]') || 'ubuntu-latest' }}"
    assert f"runs-on: {runner}" in text
    assert not re.search(r"runs-on:\s*ubuntu-latest", text)
    assert "--leg fake" in text and "--allow-mutating none" in text
    assert "--leg elevenlabs" not in text and "--leg phone" not in text
    assert "--fail-on any" in text
    assert "upload skipped: not configured" in text
    assert re.search(r"^permissions:\n  contents: read$", text, re.MULTILINE)
    # The check runs before the batch does.
    assert text.index("ci_gate check") < text.index("synthiq-caller batch")


# --- a set that cannot be graded is refused before any call (strict validate) -----


def test_a_set_with_a_scenario_that_states_no_expectations_is_refused(tmp_path: Path) -> None:
    write_scenario(tmp_path, "graded", "none")
    write_scenario(tmp_path, "bare", "none", expectations=False)
    with pytest.raises(GateRefused, match=r"bare\.json.*states no expectations"):
        ci_gate.check_scenarios(tmp_path)


def test_the_command_exits_one_for_an_ungradeable_set_and_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_scenario(tmp_path / "set", "bare", "none", expectations=False)
    args = ["check", "--target", "t", "--allowed", "t", "--root", str(tmp_path), "--set", "set"]
    assert ci_gate.main(args) == 1
    assert "bare.json" in capsys.readouterr().err


def test_the_committed_smoke_set_is_gradeable() -> None:
    assert ci_gate.check_scenarios(SETS / "smoke") == 2


def test_the_workflow_validates_strictly_before_the_batch() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    strict = text.index("synthiq-caller validate")
    assert "--strict" in text[strict : strict + 120]
    assert text.index("ci_gate check") < strict < text.index("synthiq-caller batch")


# --- a chain with a call that states no expectations ------------------------------


def write_chain(folder: Path, second_expects: bool) -> None:
    def call(call_id: str, expects: bool) -> dict[str, Any]:
        return {
            "id": call_id,
            "mutates": "none",
            "persona": {"facts": {"name": "Alice Wonderland"}},
            "steps": [{"when": "how can i help", "clips": ["ci-opener"]}],
            "hangup_after": "mobile number",
            "max_seconds": 20,
            "expect": {"tools": [{"name_contains": "lookup"}] if expects else [], "fields": []},
        }

    data = {"name": "look up twice", "chain": [call("first", True), call("second", second_expects)]}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "chain.json").write_text(json.dumps(data), encoding="utf-8")


def test_a_chain_call_that_states_nothing_never_loads(tmp_path: Path) -> None:
    """A chain cannot be ungradeable: ``parse_chain`` refuses any call with no expectation.

    (``chain.py``: "chain call ... expects nothing"). So there is no chain case in
    ``validate --strict``; a chain that would be one is already an error, with or
    without the flag, and it names the step.
    """
    write_chain(tmp_path, second_expects=False)
    with pytest.raises(ScenarioError, match="chain call 'second' expects nothing"):
        load_chains(tmp_path)


@pytest.mark.parametrize("strict", [[], ["--strict"]])
def test_validate_prints_a_clean_error_for_a_chain_call_that_expects_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], strict: list[str]
) -> None:
    write_chain(tmp_path, second_expects=False)
    assert main(["validate", str(tmp_path), *strict]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ERROR: chain.json: chain call 'second' expects nothing")
    assert "Traceback" not in out


def test_validate_strict_passes_a_chain_whose_every_call_states_expectations(
    tmp_path: Path,
) -> None:
    write_chain(tmp_path, second_expects=True)
    assert main(["validate", str(tmp_path), "--strict"]) == 0


def test_the_gate_refuses_a_chain_with_a_call_that_states_nothing(tmp_path: Path) -> None:
    write_chain(tmp_path, second_expects=False)
    with pytest.raises(GateRefused, match="cannot be loaded.*chain call 'second' expects nothing"):
        ci_gate.check_scenarios(tmp_path)
    write_chain(tmp_path, second_expects=True)
    assert ci_gate.check_scenarios(tmp_path) == 1
