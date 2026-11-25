"""The scorecard shows every outcome class on its own row, both rates, and the marks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from synthiq_caller.jsonio import write_json
from synthiq_caller.scorecard import build, render_html


def payload(
    scenario: str,
    outcome: str,
    *,
    reason: str = "",
    rehearsal: bool = False,
    synthetic: bool = False,
    unverified: bool = False,
    unavailable: bool = False,
) -> dict[str, Any]:
    calls = []
    if unavailable:
        calls.append(
            {"name": "book", "arguments": {"_unavailable": "no result"}, "result": "unavailable"}
        )
    return {
        "scenario_file": f"{scenario}.json",
        "mutates": "none",
        "repeat": 1,
        "record": {"scenario": scenario, "call_ref": "c", "timeline": [], "said": []},
        "trace": {"call_ref": "c", "tool_calls": calls, "step_path": ["A"], "termination": ""},
        "trace_error": "",
        "rehearsal": rehearsal,
        "synthetic_clips": ["s.wav"] if synthetic else [],
        "labels_unverified": ["u.wav"] if unverified else [],
        "grade": {
            "scenario": scenario,
            "call_ref": "c",
            "outcome": outcome,
            "reason": reason,
            "checks": [{"what": "tool 'book' is called", "passed": outcome == "passed"}],
            "fields": [],
        },
    }


def make(tmp_path: Path, runs: list[dict[str, Any]], *, blocked: bool = True) -> Path:
    directory = tmp_path / "d"
    directory.mkdir()
    for index, run in enumerate(runs, start=1):
        write_json(directory / f"run-{index:03d}.json", run)
    blocked_rows = (
        [{"scenario_file": "z.json", "scenario": "z", "repeat": 1, "reason": "no expectations"}]
        if blocked
        else []
    )
    write_json(directory / "batch.json", {"ran": len(runs), "blocked": blocked_rows})
    return directory


def mixed(tmp_path: Path) -> Path:
    return make(
        tmp_path,
        [
            payload("a", "passed", synthetic=True),
            payload("a", "passed", unverified=True),
            payload("a", "failed"),
            payload("b", "void", reason="the rig could not answer"),
            payload("b", "ungradeable", reason="nothing to check", unavailable=True),
            payload("c", "ungraded", reason="trace not found"),
        ],
    )


def test_every_class_has_its_own_count_and_both_rates_show_counts(tmp_path: Path) -> None:
    card = build([mixed(tmp_path)])

    assert card["classes"] == {
        "passed": 2,
        "failed": 1,
        "void": 1,
        "ungradeable": 1,
        "ungraded": 1,
        "not_run": 1,
    }
    assert card["attempted"] == 6 and card["graded"] == 3
    assert card["unavailable_calls"] == 1
    assert card["synthetic_calls"] == 1
    assert card["labels_unverified_calls"] == 1
    page = render_html(card)
    assert "2 of 3 graded" in page and "2 of 6 attempted" in page
    for label in ("Void (rig could not answer)", "Ungradeable", "Ungraded", "Not run"):
        assert label in page
    assert "no expectations" in page and "trace not found" in page
    assert "Unavailable" in page
    assert "Synthetic voice" in page and "Labels unverified" in page


def test_rates_are_never_rounded_up(tmp_path: Path) -> None:
    runs = [payload("a", "passed")] * 199 + [payload("a", "failed"), payload("a", "void")]
    page = render_html(build([make(tmp_path, runs)]))
    # 199 of 200 graded is 99.5%; 199 of 201 attempted is 99.0%. Never shown as 100%.
    assert "199 of 200 graded" in page and "199 of 201 attempted" in page
    assert "99.5%" in page and "100.0%" not in page and "(100%" not in page


def test_a_rehearsal_is_marked_and_a_real_run_is_not(tmp_path: Path) -> None:
    one = tmp_path / "one"
    one.mkdir()
    rehearsal = render_html(build([make(one, [payload("a", "passed", rehearsal=True)])]))
    assert "REHEARSAL (scripted agent)" in rehearsal
    two = tmp_path / "two"
    two.mkdir()
    real = render_html(build([make(two, [payload("a", "passed")])]))
    assert "REHEARSAL" not in real


def test_nothing_unavailable_and_nothing_not_run_counts_zero(tmp_path: Path) -> None:
    card = build([make(tmp_path, [payload("a", "passed")], blocked=False)])
    assert card["classes"]["not_run"] == 0 and card["unavailable_calls"] == 0
    assert card["rehearsals"] == 0
