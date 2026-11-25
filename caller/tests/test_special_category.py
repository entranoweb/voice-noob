"""A real caller's clip is special-category data: it needs a lawful basis and a consent record
before anything plays it, and only its own switch lifts that. A synthetic clip needs neither.
Also: a failed check in a forgotten call is still named, with no value in its name."""

import json
from pathlib import Path

import pytest
from test_batch_and_report import FAST, agent, scenario
from test_clip_consent import FULL, write_bank
from test_forget_review import SENTINEL, phone_args
from test_phone_leg import scenario_file
from test_reset_chains_readback import BOOKED, CANCELLED, agents, chain_data

from synthiq_caller import cli
from synthiq_caller.batch import load_chains, run_batch
from synthiq_caller.clips import ClipBank, ClipExcluded
from synthiq_caller.consent import Consent
from synthiq_caller.grading import grade
from synthiq_caller.phone_server import main as phone_main
from synthiq_caller.scenario import parse_scenario

NO_BASIS = {**FULL, "lawful_basis": ""}
NO_CONSENT = {**FULL, "consent_ref": ""}
SYNTHETIC = {"provenance": "synthetic"}


@pytest.mark.parametrize("row", [NO_BASIS, NO_CONSENT], ids=["no basis", "no consent_ref"])
def test_a_real_clip_without_both_is_refused_by_the_bank(
    row: dict[str, str], tmp_path: Path
) -> None:
    bank = ClipBank(write_bank(tmp_path, {"opener": row}))
    assert "consent record missing" in bank.refusal("opener")
    with pytest.raises(ClipExcluded, match="consent record missing"):
        bank.pcm("opener", 8000)
    assert ClipBank(tmp_path, allow_missing_consent=True).refusal("opener") == ""


def test_a_real_clip_with_both_plays_and_is_special_category(tmp_path: Path) -> None:
    bank = ClipBank(write_bank(tmp_path, {"opener": FULL}))
    assert bank.refusal("opener") == ""
    info = bank.info("opener")
    assert info is not None and info.special_category is True
    assert Consent.from_row(FULL).special_category is True


def test_a_synthetic_clip_needs_neither(tmp_path: Path) -> None:
    row = {**SYNTHETIC, "lawful_basis": "", "consent_ref": ""}
    bank = ClipBank(write_bank(tmp_path, {"opener": row}))
    assert bank.refusal("opener") == ""
    info = bank.info("opener")
    assert info is not None and info.special_category is False


def test_unknown_provenance_does_not_lift_the_consent_rule(tmp_path: Path) -> None:
    bank = ClipBank(write_bank(tmp_path, {"opener": NO_BASIS}), allow_unknown_provenance=True)
    assert "consent record missing" in bank.refusal("opener")


def test_any_non_empty_basis_is_accepted(tmp_path: Path) -> None:
    row = {**FULL, "lawful_basis": "art6 legitimate interests; art9 2(h) health care"}
    assert Consent.from_row(row).problems() == []
    assert ClipBank(write_bank(tmp_path, {"opener": row})).refusal("opener") == ""


async def test_a_batch_will_not_play_it_and_the_override_marks_the_result(
    tmp_path: Path,
) -> None:
    bank = write_bank(tmp_path / "bank", {"opener": NO_CONSENT, "dob": FULL})
    refused = await run_batch(
        [("lookup.json", scenario("lookup"))],
        agent,
        ClipBank(bank),
        tmp_path / "refused",
        turn_settings=FAST,
        allow_unknown_provenance=True,
    )
    assert not list((tmp_path / "refused").glob("run-*.json"))
    assert any("lawful basis or consent record" in b.reason for b in refused.blocked)
    out = tmp_path / "allowed"
    await run_batch(
        [("lookup.json", scenario("lookup"))],
        agent,
        ClipBank(bank),
        out,
        turn_settings=FAST,
        allow_missing_consent=True,
    )
    stored = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert stored["consent_missing"] == ["opener.wav: no consent_ref"]


async def test_chains_obey_the_same_gate(tmp_path: Path) -> None:
    bank = write_bank(
        tmp_path / "bank",
        {
            "opener": NO_BASIS,
            "dob": FULL,
            "cancel": FULL,
            "days/wednesday-3rd": FULL,
        },
    )
    directory = tmp_path / "chains"
    directory.mkdir()
    (directory / "chain.json").write_text(json.dumps(chain_data()), encoding="utf-8")
    out = tmp_path / "out"
    result = await run_batch(
        [],
        agents([[BOOKED], [CANCELLED]]),
        ClipBank(bank),
        out,
        chains=load_chains(directory),
        allow_mutating="all",
        turn_settings=FAST,
    )
    assert not list(out.glob("run-*.json"))
    assert any("consent record" in b.reason for b in result.blocked)


def test_validate_fails_on_it_unless_overridden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank = write_bank(tmp_path / "bank", {"opener": NO_BASIS, "dob": FULL})
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "lookup.json").write_text(
        json.dumps(
            {
                "name": "lookup",
                "mutates": "none",
                "steps": [{"when": "how can i help", "clips": ["opener.wav"]}],
                "expect": {"tools": [], "fields": []},
            }
        ),
        encoding="utf-8",
    )
    check = ["validate", str(scenarios), "--clips", str(bank)]
    assert cli.main(check) == 1
    out = capsys.readouterr().out
    assert "CONSENT RECORD MISSING" in out and "--allow-missing-consent" in out
    assert cli.main([*check, "--allow-missing-consent"]) == 0


def test_the_phone_command_has_the_same_gate(tmp_path: Path) -> None:
    write_bank(tmp_path, {"opener": NO_BASIS})
    scenario_file(tmp_path)
    with pytest.raises(SystemExit):
        phone_main(phone_args(tmp_path, "--allow-unknown-provenance"), env={})
    assert phone_main(phone_args(tmp_path, "--allow-missing-consent"), env={}) == 0


def test_the_check_labels_hold_no_value() -> None:
    wanted = parse_scenario(
        {
            "name": "x",
            "mutates": "none",
            "persona": {"facts": {"name": SENTINEL}},
            "steps": [{"when": "name", "clips": ["opener.wav"], "field": "name"}],
            "expect": {
                "tools": [{"name_contains": "check", "arguments": {"name": SENTINEL}}],
                "fields": ["name"],
                "agent_says": [SENTINEL],
                "read_back": {"name": SENTINEL},
            },
        }
    )
    from synthiq_caller.director import RunRecord
    from synthiq_caller.traces.base import Trace

    graded = grade(wanted, RunRecord(scenario="x", call_ref="c"), Trace(call_ref="c"), {"a": "b"})
    assert graded.checks and all(c.label for c in graded.checks)
    for check in graded.checks:
        assert SENTINEL not in check.label, check.label
    assert any(SENTINEL in c.what for c in graded.checks)
    labels = [c.label for c in graded.checks]
    assert (
        "tool check called and succeeds" in labels and "field name reaches a tool as said" in labels
    )


def test_the_ci_gate_tone_bank_needs_no_consent_override(tmp_path: Path) -> None:
    from synthiq_caller.ci_gate import make_bank

    scenarios = tmp_path / "set"
    scenarios.mkdir()
    (scenarios / "a.json").write_text(
        json.dumps(
            {
                "name": "a",
                "steps": [{"when": "how can i help", "clips": ["opener"]}],
                "expect": {"agent_says": ["x"]},
            }
        ),
        encoding="utf-8",
    )
    bank_dir = tmp_path / "clips"
    names = make_bank(scenarios, bank_dir)
    bank = ClipBank(bank_dir)
    assert names
    assert not bank.consent_missing([f"{n}.wav" for n in names])
    assert all("consent" not in (bank.refusal(f"{n}.wav") or "") for n in names)


def test_ingested_clips_are_real_and_refused_until_both_fields_are_filled(tmp_path: Path) -> None:
    from synthiq_caller.ingest import EXTRA_COLUMNS, MANIFEST_COLUMNS, _write_manifest

    assert "lawful_basis" in EXTRA_COLUMNS and "consent_ref" in EXTRA_COLUMNS
    row = {c: "" for c in MANIFEST_COLUMNS}
    row["clip"] = "opener"
    _write_manifest(tmp_path / "manifest.csv", [row])
    (tmp_path / "opener.wav").write_bytes(b"RIFF")
    bank = ClipBank(tmp_path)
    assert "consent record missing" in (bank.refusal("opener.wav") or "")
