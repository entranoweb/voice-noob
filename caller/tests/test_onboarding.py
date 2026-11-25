"""The onboarding kit: ``init``, ``doctor``, the scenario reference, and the documented path."""

import dataclasses
import json
import re
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from synthiq_caller import cli, onboarding, reference
from synthiq_caller import scenario as schema
from synthiq_caller.traces.elevenlabs import API

DOCS = Path(__file__).resolve().parent.parent / "docs"
SECRET = "sk-test-value-that-must-never-be-printed"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to open a connection fails the test."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


# --- the scenario reference ---------------------------------------------------


def test_the_committed_reference_page_is_the_generated_one() -> None:
    committed = (DOCS / "SCENARIO-REFERENCE.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == reference.render(), (
        "docs/SCENARIO-REFERENCE.md is out of date with the scenario schema. Regenerate: "
        "synthiq-caller reference --out docs/SCENARIO-REFERENCE.md"
    )


def test_the_check_flag_fails_on_a_page_that_differs(tmp_path: Path, capsys: Any) -> None:
    page = tmp_path / "page.md"
    page.write_text(reference.render() + "an edit by hand\n", encoding="utf-8")
    assert cli.main(["reference", "--check", str(page)]) == 1
    assert "out of date" in capsys.readouterr().out
    page.write_text(reference.render(), encoding="utf-8")
    assert cli.main(["reference", "--check", str(page)]) == 0


def test_every_schema_field_has_a_meaning_and_no_meaning_outlives_its_field() -> None:
    fields = reference.documented_names()
    assert fields - set(reference.MEANING) - set(reference.DERIVED) == set(), "add a MEANING"
    assert set(reference.MEANING) - fields == set(), "a MEANING names a field that is gone"
    assert set(reference.DERIVED) <= fields


def test_every_schema_class_is_on_the_page() -> None:
    dataclass_names = {
        name
        for name, value in vars(schema).items()
        if dataclasses.is_dataclass(value) and isinstance(value, type)
    }
    assert dataclass_names == {cls.__name__ for cls, _ in reference.SECTIONS}


def test_a_field_added_to_the_schema_changes_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    before = reference.render()
    grown = dataclasses.make_dataclass(
        "Step",
        [("added_later", str, dataclasses.field(default=""))],
        bases=(schema.Step,),
        frozen=True,
    )
    monkeypatch.setitem(reference.MEANING, "Step.added_later", "A field somebody added.")
    monkeypatch.setattr(
        reference,
        "SECTIONS",
        tuple((grown if cls is schema.Step else cls, where) for cls, where in reference.SECTIONS),
    )
    after = reference.render()
    assert after != before and "`added_later`" in after


def test_the_page_lists_the_allowed_values_the_parser_enforces() -> None:
    page = reference.render()
    for value in (*schema.MUTATION_LEVELS, *schema.KNOWN_FIELDS, *schema.WEEKDAYS):
        assert f"`{value}`" in page


def test_the_page_names_each_key_by_the_name_the_parser_reads() -> None:
    page = reference.render()
    # Attribute names that differ from the key in a file.
    assert "`persona.facts`" in page and "`facts`" not in page.split("| Key")[1].split("\n")[2]
    for spelling in ("gap", "name", "type"):
        assert f"Also accepted as `{spelling}`" in page


def test_the_small_example_on_the_page_parses() -> None:
    parsed = schema.parse_scenario(reference.EXAMPLE)
    assert parsed.states_expectations() and parsed.mutates == "none"


def test_a_scenario_using_every_key_on_the_page_parses() -> None:
    """Each key the page documents is one the parser accepts and reads."""
    data: dict[str, Any] = {
        "name": "everything",
        "persona": {"facts": {"name": "alex example"}},
        "steps": [
            {
                "when": "name",
                "clips": ["a"],
                "dtmf": "1",
                "intended": "x",
                "field": "name",
                "gap_s": 0.5,
                "delay_s": 0.5,
                "talk_over": True,
                "stay_silent": False,
                "max_uses": 2,
            }
        ],
        "hangup_after": "bye",
        "max_seconds": 30,
        "expect": {
            "tools": [
                {
                    "name_contains": "x",
                    "name": "xy",
                    "ok": False,
                    "called": True,
                    "arguments": {"a": "b"},
                    "outputs": {"c": "d"},
                    "at_least_once": True,
                }
            ],
            "fields": ["name"],
            "agent_says": ["a"],
            "agent_never_says": ["b"],
            "read_back": {"k": "v"},
            "booking": {
                "weekdays": ["monday"],
                "date_from": "2026-01-01",
                "date_to": "2026-01-02",
                "time_from": "09:00",
                "time_to": "10:00",
                "appointment_type": "t",
                "clinician": "c",
                "status": "s",
                "timezone": "UTC",
                "naive_times": "UTC",
                "tool": "book",
                "start_keys": ["s"],
                "type_keys": ["t"],
                "clinician_keys": ["c"],
                "status_keys": ["z"],
            },
        },
        "stored_keys": {"name": ["n"]},
        "mutates": "books",
        "requires": "r",
        "missing_clips": ["m"],
        "description": "d",
        "clean_state": True,
        "fallback": {"clips": ["a"], "intended": "i", "gap_s": 1, "max_uses": 3},
        "identity_budget": 2,
        "identity_attempts": 2,
        "tool_outcomes": {"book": {"path": "outcome", "success": ["BOOKED"]}},
        "agent_may_end": True,
        "fixture_faults": {"book": ["DUPLICATE"]},
    }
    parsed = schema.parse_scenario(data)
    for f in dataclasses.fields(schema.Scenario):
        if f.name == "missing_target":
            continue
        assert getattr(parsed, f.name) != f.default, f"{f.name} was not read from its key"


# --- init -----------------------------------------------------------------------


def test_init_writes_the_folder_and_refuses_to_overwrite_it(tmp_path: Path, capsys: Any) -> None:
    target = tmp_path / "target"
    assert cli.main(["init", str(target)]) == 0
    for name in (
        ".gitignore",
        "target.json",
        "env.example",
        "clips/manifest.csv",
        "scenarios/01-asks-opening-hours.json",
        "scenarios/02-lookup-by-date-of-birth.json",
        "scenarios/03-lookup-by-name.json",
    ):
        assert (target / name).is_file(), name
    ignore = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"cases/", "clips/", "*.wav", "*.mp3"} <= set(ignore)

    (target / "target.json").write_text('{"name": "mine"}', encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["init", str(target)]) == 2
    assert "nothing was written" in capsys.readouterr().out
    assert json.loads((target / "target.json").read_text(encoding="utf-8")) == {"name": "mine"}

    assert cli.main(["init", str(target), "--force"]) == 0
    assert json.loads((target / "target.json").read_text(encoding="utf-8"))["name"] != "mine"


def test_the_scaffold_holds_no_name_number_host_or_key(tmp_path: Path) -> None:
    onboarding.init(tmp_path)
    text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in tmp_path.rglob("*")
        if p.is_file() and p.suffix in {".json", ".csv", ".example", ""}
    )
    assert not re.search(r"https?://", text)
    assert not re.search(r"\+?\d{9,}", text)
    assert "sk-" not in text and "_key" not in text.lower().replace("api_key=", "")


def test_the_scaffolded_scenarios_are_read_only_and_state_what_they_expect(tmp_path: Path) -> None:
    onboarding.init(tmp_path)
    files = sorted((tmp_path / "scenarios").glob("*.json"))
    assert len(files) == 3
    for file in files:
        parsed = schema.load_scenario(file)
        assert parsed.mutates == "none" and parsed.states_expectations(), file.name


# --- doctor ---------------------------------------------------------------------


def statuses(checks: list[onboarding.Check]) -> dict[str, str]:
    return {c.name: c.status for c in checks}


def test_doctor_never_prints_the_key_and_says_whether_it_is_set(
    monkeypatch: pytest.MonkeyPatch, capsys: Any, no_network: None
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SECRET)
    assert cli.main(["doctor"]) in (0, 1)  # ffmpeg may be absent on this machine
    shown = capsys.readouterr()
    assert SECRET not in shown.out + shown.err and "sk-test" not in shown.out
    assert "is set (value not shown)" in shown.out

    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert cli.main(["doctor"]) == 1
    assert "is not set" in capsys.readouterr().out


def test_doctor_checks_the_region_and_names_a_bad_one(no_network: None) -> None:
    assert statuses(onboarding.doctor(region="eu", fake_only=True))["region"] == "ok"
    bad = onboarding.doctor(region="mars", fake_only=True)
    assert statuses(bad)["region"] == "FAIL"
    assert "sg" in next(c.detail for c in bad if c.name == "region")


def test_doctor_touches_the_network_only_when_asked(no_network: None) -> None:
    quiet = onboarding.doctor(region="global", fake_only=False, environ={})
    assert statuses(quiet)["network"] == "skipped"

    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    checks = onboarding.doctor(
        region="eu",
        check_network=True,
        environ={"ELEVENLABS_API_KEY": SECRET},
        transport=httpx.MockTransport(answer),
    )
    assert statuses(checks)["network"] == "ok"
    assert [r.url.host for r in seen] == ["api.eu.residency.elevenlabs.io"]
    assert "xi-api-key" not in seen[0].headers and SECRET not in str(seen[0].headers)
    assert SECRET not in "\n".join(c.line() for c in checks)


def test_doctor_reports_an_address_that_does_not_answer() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    checks = onboarding.doctor(
        check_network=True, environ={}, transport=httpx.MockTransport(refuse)
    )
    assert statuses(checks)["network"] == "FAIL"


def test_doctor_checks_a_target_folder_and_that_recordings_are_ignored(tmp_path: Path) -> None:
    assert statuses(onboarding.doctor(directory=tmp_path, fake_only=True))["target.json"] == "FAIL"
    onboarding.init(tmp_path)
    good = statuses(onboarding.doctor(directory=tmp_path, fake_only=True))
    assert good["target.json"] == good["scenarios"] == good[".gitignore"] == "ok"
    (tmp_path / ".gitignore").write_text("runs/\n", encoding="utf-8")
    assert statuses(onboarding.doctor(directory=tmp_path, fake_only=True))[".gitignore"] == "FAIL"


def test_capture_takes_the_region_in_place_of_an_address(capsys: Any, no_network: None) -> None:
    arguments = ["capture", "--trace", "elevenlabs", "--conversation", "c1", "--out", "x"]
    code = cli.main([*arguments, "--region", "eu", "--api", "https://elsewhere.invalid/v1"])
    assert code == 2 and "different addresses" in capsys.readouterr().out
    assert onboarding.REGIONS["global"] == API


# --- the documented path, end to end ----------------------------------------------


def test_the_documented_path_reaches_a_graded_run_on_the_fake_leg(
    tmp_path: Path, capsys: Any, no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """init, validate, a batch on the fake leg: graded, no network, no key."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    target = tmp_path / "target"
    assert cli.main(["init", str(target)]) == 0
    monkeypatch.chdir(target)

    assert cli.main(["doctor", ".", "--fake-only"]) == 0
    capsys.readouterr()

    assert cli.main(["validate", "scenarios", "--clips", "clips", "--target", "target.json"]) == 0
    validated = capsys.readouterr().out
    assert validated.count("mutates=none") == 3
    assert "CLIPS NOT USABLE" not in validated and "UNGRADEABLE" not in validated

    arguments = ["batch", "scenarios", "--leg", "fake", "--fake-agent", "agent-scripts"]
    arguments += ["--clips", "clips", "--target", "target.json", "--out", "runs", "--settle", "0"]
    assert cli.main(arguments) == 0

    runs = sorted(Path("runs").glob("run-*.json"))
    assert len(runs) == 3
    outcomes = [json.loads(p.read_text(encoding="utf-8"))["grade"]["outcome"] for p in runs]
    assert all(o in ("passed", "failed") for o in outcomes), outcomes
    # What this proves is the path, and the run says so itself.
    stored = [json.loads(p.read_text(encoding="utf-8")) for p in runs]
    assert all(r["rehearsal"] is True for r in stored)
    assert all(r["leg"] == "fake" for r in stored)


def test_a_scaffolded_scenario_stripped_of_its_expectations_is_flagged_not_passed(
    tmp_path: Path, capsys: Any
) -> None:
    onboarding.init(tmp_path)
    path = tmp_path / "scenarios" / "01-asks-opening-hours.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expect"] = {}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert (
        cli.main(["validate", str(tmp_path / "scenarios"), "--clips", str(tmp_path / "clips")]) == 0
    )
    assert "UNGRADEABLE until expectations are stated" in capsys.readouterr().out


def test_a_wrong_value_in_the_script_fails_the_run_so_a_graded_pass_means_something(
    tmp_path: Path, no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    onboarding.init(tmp_path)
    script = tmp_path / "agent-scripts" / "02-lookup-by-date-of-birth.json"
    script.write_text(
        script.read_text(encoding="utf-8").replace("2000-01-01", "1999-02-02"), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    arguments = ["batch", "scenarios", "--leg", "fake", "--fake-agent", "agent-scripts"]
    arguments += ["--clips", "clips", "--target", "target.json", "--out", "runs", "--settle", "0"]
    assert cli.main([*arguments, "--only", "02"]) == 0
    [run] = sorted(Path("runs").glob("run-*.json"))
    assert json.loads(run.read_text(encoding="utf-8"))["grade"]["outcome"] == "failed"


def _source() -> str:
    root = Path(__file__).resolve().parent.parent / "src" / "synthiq_caller"
    return "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))


def test_the_guide_uses_real_commands_and_promises_nothing_that_does_not_exist() -> None:
    guide = (DOCS / "ONBOARDING.md").read_text(encoding="utf-8")
    used = set(re.findall(r"^\s*synthiq-caller ([a-z-]+)", guide, re.M))
    known = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', _source()))
    assert used and used <= known, used - known
    # Neutral placeholders only: no addresses, keys or numbers.
    assert not re.search(r"https?://(?!docs\.astral\.sh)", guide)
    assert not re.search(r"sk-|agent_[0-9a-z]{6,}|\+?\d{9,}", guide)
    # The strict flag is announced as coming, and no command line in the guide uses it.
    assert "does not exist yet" in guide and "--strict" in guide
    assert not re.search(r"^\s*synthiq-caller .*--strict", guide, re.M)
    assert '"--strict"' not in _source().replace('"--strict" not in', "")


def test_the_guide_names_the_consent_fields_for_real_clips() -> None:
    guide = (Path(__file__).resolve().parents[1] / "docs" / "ONBOARDING.md").read_text("utf-8")
    assert "lawful_basis" in guide
    assert "consent_ref" in guide


def test_the_tones_init_writes_are_synthetic_and_need_no_consent(tmp_path: Path) -> None:
    import csv

    target = tmp_path / "t"
    assert cli.main(["init", str(target)]) == 0
    with (target / "clips" / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert all(row["provenance"].startswith("synthetic") for row in rows)
    assert all(not row.get("lawful_basis") and not row.get("consent_ref") for row in rows)
