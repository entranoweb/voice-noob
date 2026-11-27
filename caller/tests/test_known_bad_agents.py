"""C5: the rig fails each known-bad scripted agent, and still passes the good one.

Every bad agent under ``ci/agents/bad`` models one failure class with the fake leg
(no network, no real agent). A rig that passed any of them would be passing
broken agents; one that failed the good one would be failing working ones.
"""

import json
from pathlib import Path

import pytest
from test_ci_gate import AGENTS, SETS

from synthiq_caller import ci_gate
from synthiq_caller.cli import main

BAD = AGENTS / "bad"
CLASSES = {
    "never-calls-the-tool",
    "stores-the-wrong-value",
    "tool-call-fails",
    "hangs-up-before-asking",
    "never-asks-for-the-date",
    "writes-in-a-read-only-call",
}
# A failure of this class is only visible against a profile that says which tools write.
NEEDS_WRITE_LIST = {"writes-in-a-read-only-call"}


def outcomes(tmp_path: Path, agent: Path, *extra: str) -> list[str]:
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
            "--settle",
            "0",
            "--only",
            "01-",
            *extra,
        ]
    )
    runs = sorted(out.glob("run-*.json"))
    assert runs, "the batch placed no call, so nothing was judged"
    return [json.loads(p.read_text(encoding="utf-8"))["grade"]["outcome"] for p in runs]


def test_every_failure_class_has_an_agent_and_nothing_else_is_there() -> None:
    assert {p.stem for p in BAD.glob("*.json")} == CLASSES


def test_the_good_agent_passes(tmp_path: Path) -> None:
    assert set(outcomes(tmp_path, AGENTS / "smoke")) == {"passed"}


def test_the_good_agent_passes_with_the_write_list_too(tmp_path: Path) -> None:
    profile = tmp_path / "target.json"
    profile.write_text(json.dumps({"name": "rehearsal", "write_tools": ["book"]}))
    assert set(outcomes(tmp_path, AGENTS / "smoke", "--target", str(profile))) == {"passed"}


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_each_known_bad_agent_fails(tmp_path: Path, name: str) -> None:
    extra: list[str] = []
    if name in NEEDS_WRITE_LIST:
        profile = tmp_path / "target.json"
        profile.write_text(json.dumps({"name": "rehearsal", "write_tools": ["book"]}))
        extra = ["--target", str(profile)]
    assert outcomes(tmp_path, BAD / f"{name}.json", *extra) == ["failed"]
