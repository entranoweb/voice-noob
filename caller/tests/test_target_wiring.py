"""A target profile that wires reset, hooks and read-back; chains reading it; the dry run.

Scripted agent and fake database only. Nothing here reaches a real system.
"""

import importlib.util
import json
import sys
import wave
from pathlib import Path
from typing import Any

import pytest
from conftest import mark_checked

from synthiq_caller.batch import ShellHooks, load_chains, regrade_chains, run_batch
from synthiq_caller.chain import parse_chain
from synthiq_caller.cli import main
from synthiq_caller.clips import ClipBank
from synthiq_caller.leg import Leg
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.readback import CommandReadBack
from synthiq_caller.report import summarise
from synthiq_caller.reset import CommandReset
from synthiq_caller.scenario import parse_scenario
from synthiq_caller.target import (
    TargetError,
    command_of,
    exceeds_allowance,
    load_target,
    parse_target,
)
from synthiq_caller.traces.base import ToolCall, Trace, TraceReader
from synthiq_caller.turns import TurnSettings
from synthiq_caller.validation import validate_directory

FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)
PY = sys.executable
CALLER = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "read_back_sql", CALLER / "scripts" / "read_back_sql.py"
)
assert _spec is not None and _spec.loader is not None
read_back_sql = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(read_back_sql)


def scenario_data(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "registers a caller",
        "mutates": "registers",
        "clean_state": True,
        "persona": {"facts": {"name": "Alice Wonderland", "date_of_birth": "2002-02-20"}},
        "steps": [
            {"when": "how can i help", "clips": ["opener.wav"]},
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
        ],
        "hangup_after": "mobile number",
        "max_seconds": 20,
        "expect": {"tools": [{"name_contains": "register"}], "fields": ["date_of_birth"]},
    }
    data.update(extra)
    return data


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    directory = tmp_path / "bank"
    directory.mkdir()
    for name in ("opener.wav", "dob.wav"):
        with wave.open(str(directory / name), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(tone(0.25))
    mark_checked(directory)
    return ClipBank(directory)


class Traces:
    def __init__(self, traces: list[list[ToolCall]]) -> None:
        self.traces = list(traces)

    async def read(self, call_ref: str) -> Trace:
        return Trace(call_ref=call_ref, tool_calls=self.traces.pop(0), step_path=["S1"])


def agents(traces: list[list[ToolCall]], made: list[int] | None = None) -> Any:
    reader = Traces(traces)

    def make() -> tuple[Leg, TraceReader | None]:
        if made is not None:
            made.append(1)
        lines = [
            AgentLine("Hi, how can I help you today?", speech_s=0.3),
            AgentLine("Could I take your date of birth?", speech_s=0.3, hears="20th February 2002"),
            AgentLine("And the best mobile number?", speech_s=0.3),
        ]
        return FakeLeg(lines), reader

    return make


REGISTERED = ToolCall("register_patient", {"dateOfBirth": "2002-02-20"}, {}, True)


def log_command(log: Path, word: str, *more: str, exit_code: int = 0) -> list[str]:
    """A command that appends one line to ``log`` and exits as told. Stands in for a tool."""
    code = (
        "import os, sys; "
        "open(sys.argv[1], 'a', encoding='utf-8').write("
        "' '.join(sys.argv[2:-1]) + ' phase=' + os.environ.get('SYNTHIQ_PHASE', '') + '\\n'); "
        "sys.exit(int(sys.argv[-1]))"
    )
    return [PY, "-c", code, str(log), word, *more, str(exit_code)]


def profile(tmp_path: Path, **parts: Any) -> dict[str, Any]:
    return parse_target({"name": "a-target", **parts})


# --- the profile ------------------------------------------------------------------


def test_a_profile_command_is_a_list_and_never_a_string() -> None:
    with pytest.raises(TargetError, match="is a list"):
        parse_target({"name": "t", "reset": {"command": "npm run reset && rm -rf x"}})
    with pytest.raises(TargetError, match="is a list"):
        parse_target({"name": "t", "read_back": {"command": []}})
    with pytest.raises(TargetError, match="timeout_s"):
        parse_target({"name": "t", "reset": {"command": ["x"], "timeout_s": 0}})
    with pytest.raises(TargetError, match="allow_mutating"):
        parse_target({"name": "t", "allow_mutating": "everything"})
    target = parse_target({"name": "t", "reset": {"command": ["tool", "restore"], "timeout_s": 9}})
    assert command_of(target, "reset") == (["tool", "restore"], 9.0)
    assert command_of(target, "read_back") == ([], None)
    assert command_of(None, "reset") == ([], None)


def test_the_profile_caps_what_a_batch_may_change_and_never_switches_it_on() -> None:
    target = parse_target({"name": "t", "allow_mutating": "registers"})
    assert not exceeds_allowance(target, "none")
    assert not exceeds_allowance(target, "registers")
    assert exceeds_allowance(target, "books") and exceeds_allowance(target, "all")
    # A profile that does not say leaves the batch's own flag as the only guard.
    assert not exceeds_allowance(parse_target({"name": "t"}), "all")


def test_the_example_profile_and_queries_in_the_repository_are_valid() -> None:
    target = load_target(CALLER / "scenarios" / "sophie" / "targets" / "wired-target.example.json")
    assert "{call_ref}" in command_of(target, "after_call")[0]
    text = json.dumps(target)
    # Placeholders only: the example names no real host, id or person.
    assert "<TEST_DATABASE_HOST>" in text and "<PRODUCT_CHECKOUT>" in text
    example = json.loads(
        (CALLER / "scripts" / "read_back_queries.example.json").read_text(encoding="utf-8")
    )
    assert len(read_back_sql.load_queries(example)["queries"]) == 2


# --- reset, adopt hook and read-back around a call ----------------------------------


async def test_reset_then_call_then_read_back_then_adopt_with_the_conversation_id(
    bank: ClipBank, tmp_path: Path
) -> None:
    log = tmp_path / "log.txt"
    stored = "import json; print(json.dumps({'date_of_birth': '2002-02-20', 'name': 'Alice Wonderland'}))"
    target = profile(
        tmp_path,
        reset={"command": log_command(log, "restore")},
        after_call={"command": log_command(log, "adopt", "--conversation", "{call_ref}")},
        read_back={"command": [PY, "-c", stored]},
    )
    out = tmp_path / "out"
    result = await run_batch(
        [("a.json", parse_scenario(scenario_data(), target, "a"))],
        agents([[REGISTERED]]),
        bank,
        out,
        allow_mutating="registers",
        reset=CommandReset(command_of(target, "reset")[0]),
        read_back=CommandReadBack(command_of(target, "read_back")[0]),
        hooks=ShellHooks(after=command_of(target, "after_call")[0]),
        turn_settings=FAST,
    )
    assert (result.ran, result.blocked) == (1, [])
    run = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    ref = run["record"]["call_ref"]
    assert ref and run["hook_error"] == ""
    # The reset first, the adopt after, and the adopt was told which conversation.
    assert log.read_text(encoding="utf-8").splitlines() == [
        "restore phase=reset",
        f"adopt --conversation {ref} phase=after",
    ]
    row = run["grade"]["fields"][0]
    assert (row["field"], row["stored"], row["stored_source"]) == (
        "date_of_birth",
        "2002-02-20",
        "read back",
    )
    assert run["grade"]["outcome"] == "passed"


async def test_a_failed_reset_command_means_not_run_and_nothing_after_it_runs(
    bank: ClipBank, tmp_path: Path
) -> None:
    log = tmp_path / "log.txt"
    made: list[int] = []
    out = tmp_path / "out"
    result = await run_batch(
        [("a.json", parse_scenario(scenario_data()))],
        agents([], made),
        bank,
        out,
        allow_mutating="registers",
        reset=CommandReset(log_command(log, "restore", exit_code=1)),
        hooks=ShellHooks(after=log_command(log, "adopt", "{call_ref}")),
        turn_settings=FAST,
    )
    assert made == [] and result.ran == 0
    assert "reset command exited 1" in result.blocked[0].reason
    assert log.read_text(encoding="utf-8").splitlines() == ["restore phase=reset"]
    summary = summarise(out)
    assert (summary.failed, summary.runs, len(summary.blocked)) == (0, 0, 1)


async def test_a_read_back_that_holds_nothing_fails_the_check_whatever_the_tool_was_sent(
    bank: ClipBank, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    nothing = "import json; print(json.dumps({'date_of_birth': None}))"
    await run_batch(
        [("a.json", parse_scenario(scenario_data(clean_state=False)))],
        agents([[REGISTERED]]),
        bank,
        out,
        allow_mutating="registers",
        read_back=CommandReadBack([PY, "-c", nothing]),
        turn_settings=FAST,
    )
    run = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    row = run["grade"]["fields"][0]
    assert (row["stored"], row["stored_source"], row["stored_matches"]) == (
        None,
        "read back",
        False,
    )
    assert run["grade"]["outcome"] == "failed"


async def test_a_hook_given_as_a_list_fills_the_reference_inside_one_argument_only(
    bank: ClipBank, tmp_path: Path
) -> None:
    log = tmp_path / "log.txt"
    out = tmp_path / "out"
    await run_batch(
        [("a.json", parse_scenario(scenario_data(clean_state=False)))],
        agents([[REGISTERED]]),
        bank,
        out,
        allow_mutating="registers",
        # An argument with spaces stays one argument: nothing splits a list again.
        hooks=ShellHooks(after=log_command(log, "adopt two words", "--id={call_ref}")),
        turn_settings=FAST,
    )
    run = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"adopt two words --id={run['record']['call_ref']} phase=after"
    ]


# --- chains read the profile ---------------------------------------------------------


def chain_data() -> dict[str, Any]:
    call: dict[str, Any] = {
        "persona": {"facts": {"name": "Alice Wonderland"}},
        "steps": [{"when": "how can i help", "clips": ["opener.wav"]}],
        "hangup_after": "date of birth",
        "max_seconds": 20,
    }
    return {
        "name": "book then look",
        "chain": [
            {
                "id": "book",
                "mutates": "books",
                **call,
                "expect": {
                    "tools": [{"name_contains": "book"}],
                    "booking": {"tool": "book"},
                    "booking_from_target": ["time_from", "time_to"],
                },
                "capture": {"appointment": {"tool": "book", "result": "appointment.id"}},
            },
            {
                "id": "look",
                "mutates": "none",
                **call,
                "expect": {"tools": [{"name_contains": "{{appointment|re}}"}]},
            },
        ],
    }


TIMES = {"time_from": "16:30", "time_to": "16:30"}


def test_a_chain_call_takes_its_appointment_from_the_profile_under_file_and_call_id() -> None:
    target = parse_target({"name": "t", "timezone": "Europe/London", "bookings": {"c/book": TIMES}})
    chain = parse_chain(chain_data(), target, "c")
    assert [c.missing_target for c in chain.calls] == [(), ()]
    booking = chain.calls[0].scenario({}).expect.booking
    assert booking is not None
    assert (booking.time_from, booking.time_to, booking.timezone) == (
        "16:30",
        "16:30",
        "Europe/London",
    )
    # Another target's times never leak in: the same chain with no profile lacks them.
    assert parse_chain(chain_data(), None, "c").calls[0].missing_target == ("time_from", "time_to")
    # Keyed by call: a profile that gives the times to a different call does not count.
    other = parse_target({"name": "t", "bookings": {"c/look": TIMES}})
    assert parse_chain(chain_data(), other, "c").calls[0].missing_target == ("time_from", "time_to")


async def test_a_chain_the_profile_does_not_cover_is_held_back_whole(
    bank: ClipBank, tmp_path: Path
) -> None:
    made: list[int] = []
    out = tmp_path / "out"
    result = await run_batch(
        [],
        agents([], made),
        bank,
        out,
        chains=[("c.json", parse_chain(chain_data(), parse_target({"name": "t"}), "c"))],
        allow_mutating="all",
        turn_settings=FAST,
    )
    assert made == [] and result.ran == 0
    assert all("target profile does not give" in b.reason for b in result.blocked)
    stored = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
    assert stored["outcome"] == "not run"


def test_load_validate_and_regrade_all_hand_the_profile_to_chains(tmp_path: Path) -> None:
    directory = tmp_path / "scenarios"
    directory.mkdir()
    (directory / "c.json").write_text(json.dumps(chain_data()), encoding="utf-8")
    target = parse_target({"name": "t", "bookings": {"c/book": TIMES}})
    assert load_chains(directory, target)[0][1].calls[0].missing_target == ()
    assert load_chains(directory)[0][1].calls[0].missing_target == ("time_from", "time_to")
    bare = validate_directory(directory, None, None, None)[0]
    assert bare.missing_target == ["book: time_from", "book: time_to"]
    assert "TARGET PROFILE LACKS" in bare.line()
    assert validate_directory(directory, None, None, target)[0].missing_target == []
    # Nothing stored yet: regrading with the profile is a no-op, not an error.
    assert regrade_chains(tmp_path, load_chains(directory, target)) == 0


# --- the dry run ---------------------------------------------------------------------


def write_setup(tmp_path: Path, log: Path, **parts: Any) -> tuple[Path, Path]:
    directory = tmp_path / "scenarios"
    directory.mkdir()
    (directory / "a.json").write_text(json.dumps(scenario_data()), encoding="utf-8")
    (directory / "b.json").write_text(
        json.dumps(scenario_data(name="books", mutates="books")), encoding="utf-8"
    )
    target = {
        "name": "a-target",
        "allow_mutating": "registers",
        "reset": {"command": log_command(log, "restore")},
        "after_call": {"command": log_command(log, "adopt", "--conversation", "{call_ref}")},
        "read_back": {"command": log_command(log, "read")},
        **parts,
    }
    path = tmp_path / "target.json"
    path.write_text(json.dumps(target), encoding="utf-8")
    return directory, path


def test_a_dry_run_prints_every_command_and_runs_none(
    bank: ClipBank, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log.txt"
    directory, target = write_setup(tmp_path, log)
    out = tmp_path / "out"
    code = main(
        ["batch", str(directory), "--leg", "fake", "--clips", str(bank.directory), "--out", str(out),
         "--target", str(target), "--allow-mutating", "registers", "--dry-run"]
    )  # fmt: skip
    printed = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in printed and "target profile: a-target" in printed
    # Each command, as the list it would be run as.
    for word in ("'restore'", "'adopt'", "'--conversation'", "'{call_ref}'", "'read'"):
        assert word in printed
    assert "reset:" in printed and "read back:" in printed and "after:" in printed
    # The scenario above the allowance is named as held back, with the reason.
    assert "b.json" in printed and "NOT RUN: mutation guard: scenario books" in printed
    assert "1 of 2 file(s) would be run" in printed
    # Nothing ran, nothing was written, and no fake agent was needed.
    assert not log.exists() and not out.exists()


def test_a_batch_asking_for_more_than_the_profile_permits_is_refused_before_anything_runs(
    bank: ClipBank, tmp_path: Path
) -> None:
    log = tmp_path / "log.txt"
    directory, target = write_setup(tmp_path, log)
    with pytest.raises(SystemExit, match="more than the target profile"):
        main(
            ["batch", str(directory), "--leg", "fake", "--clips", str(bank.directory),
             "--out", str(tmp_path / "out"), "--target", str(target),
             "--allow-mutating", "books", "--dry-run"]
        )  # fmt: skip
    assert not log.exists()


def test_the_command_line_wins_over_the_profile(
    bank: ClipBank, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log.txt"
    directory, target = write_setup(tmp_path, log)
    main(
        ["batch", str(directory), "--leg", "fake", "--clips", str(bank.directory),
         "--out", str(tmp_path / "out"), "--target", str(target), "--allow-mutating", "registers",
         "--reset", "none", "--read-back", "none", "--after-call", "other-tool {call_ref}",
         "--dry-run"]
    )  # fmt: skip
    printed = capsys.readouterr().out
    assert "already clean" in printed and "'restore'" not in printed
    assert "['other-tool', '{call_ref}']" in printed and "'adopt'" not in printed
    assert "read back:  none" in printed


# --- the read-back script ------------------------------------------------------------


class Column:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeCursor:
    def __init__(self, database: "FakeDatabase") -> None:
        self.database = database
        self.row: tuple[Any, ...] | None = None
        self.description: list[Column] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, values: Any = None) -> None:
        self.database.executed.append((sql, values))
        columns, self.row = self.database.answer(sql)
        self.description = [Column(c) for c in columns]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class FakeDatabase:
    """Answers by the first table a query names. Records everything it was sent."""

    def __init__(self, rows: dict[str, tuple[list[str], tuple[Any, ...] | None]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, Any]] = []
        self.closed = False
        self.dsn = ""

    def answer(self, sql: str) -> tuple[list[str], tuple[Any, ...] | None]:
        for table, found in self.rows.items():
            if f"from {table}" in sql:
                return found
        return ["set_config"], ("",)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True

    def connect(self, dsn: str) -> "FakeDatabase":
        self.dsn = dsn
        return self


QUERIES = {
    "settings": {"app.scope": "scope_id"},
    "sanity": "select 1 from tenants where id = %(scope_id)s",
    "queries": [
        {
            "name": "patient",
            "needs": ["fact_name"],
            "columns": ["name", "date_of_birth", "phone"],
            "sql": "select full_name as name, dob as date_of_birth, phone from patients "
            "where tenant_id = %(scope_id)s and full_name = %(fact_name)s",
        },
        {
            "name": "appointment",
            "needs": ["call_ref"],
            "columns": ["appointment_status"],
            "sql": "select status as appointment_status from appointments "
            "where tenant_id = %(scope_id)s and call_ref = %(call_ref)s",
        },
    ],
}
ENV = {
    "SYNTHIQ_READBACK_DSN": "postgresql://reader:pw-not-to-print@db.invalid/app",
    "SYNTHIQ_READBACK_SCOPE_ID": "scope-1",
    "SYNTHIQ_FACT_NAME": "Alice Wonderland",
    "SYNTHIQ_CALL_REF": "conv_9",
}


def queries_file(tmp_path: Path, data: dict[str, Any] | None = None) -> str:
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(data or QUERIES), encoding="utf-8")
    return str(path)


def test_the_script_returns_the_stored_patient_and_appointment_held_to_one_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import datetime as dt

    database = FakeDatabase(
        {
            "tenants": (["?column?"], (1,)),
            "patients": (
                ["name", "date_of_birth", "phone"],
                ("Alice Wonderland", dt.date(2002, 2, 20), None),
            ),
            "appointments": (["appointment_status"], ("booked",)),
        }
    )
    code = read_back_sql.main(["--queries", queries_file(tmp_path)], ENV, database.connect)
    assert code == 0 and database.closed
    assert json.loads(capsys.readouterr().out) == {
        "name": "Alice Wonderland",
        "date_of_birth": "2002-02-20",
        "phone": None,  # asked and held nothing: the grader counts it as not stored
        "appointment_status": "booked",
    }
    # Every statement carried the scope as a parameter; no value is in the query text.
    data = [(sql, values) for sql, values in database.executed if "set_config" not in sql]
    assert len(data) == 3
    assert all(values["scope_id"] == "scope-1" for _, values in data)
    assert all("scope-1" not in sql and "Alice" not in sql for sql, _ in database.executed)
    assert ("select set_config(%s, %s, true)", ("app.scope", "scope-1")) in database.executed


def test_no_row_answers_null_for_its_columns_and_an_unknown_need_skips_the_query(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = FakeDatabase(
        {"tenants": (["?column?"], (1,)), "patients": (["name", "date_of_birth", "phone"], None)}
    )
    env = {k: v for k, v in ENV.items() if k != "SYNTHIQ_CALL_REF"}
    assert read_back_sql.main(["--queries", queries_file(tmp_path)], env, database.connect) == 0
    # The patient was asked for and is not there; the appointment was never asked for.
    assert json.loads(capsys.readouterr().out) == {
        "name": None,
        "date_of_birth": None,
        "phone": None,
    }
    assert not any("from appointments" in sql for sql, _ in database.executed)


def test_a_scope_the_role_cannot_see_is_a_failed_read_back_not_an_empty_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = FakeDatabase({"tenants": (["?column?"], None)})
    code = read_back_sql.main(["--queries", queries_file(tmp_path)], ENV, database.connect)
    captured = capsys.readouterr()
    assert code == 3 and captured.out == "" and "sanity query found nothing" in captured.err
    assert not any("from patients" in sql for sql, _ in database.executed)


def test_the_script_refuses_without_a_scope_and_never_prints_the_connection_string(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(dsn: str) -> Any:
        raise AssertionError("must not connect")

    env = {k: v for k, v in ENV.items() if k != "SYNTHIQ_READBACK_SCOPE_ID"}
    assert read_back_sql.main(["--queries", queries_file(tmp_path)], env, never) == 2
    assert "SCOPE_ID is not set" in capsys.readouterr().err

    def broken(dsn: str) -> Any:
        raise RuntimeError(f"could not connect to {dsn}")

    assert read_back_sql.main(["--queries", queries_file(tmp_path)], ENV, broken) == 3
    captured = capsys.readouterr()
    assert "pw-not-to-print" not in captured.err + captured.out and "RuntimeError" in captured.err


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("select 1 from patients where full_name = %(fact_name)s", "scope_id"),
        ("delete from patients where tenant_id = %(scope_id)s", "plain select"),
        ("select 1 from t where id = %(scope_id)s; drop table t", "plain select"),
        ("select 1 from t where id = %(scope_id)s -- and more", "plain select"),
        ("select 1 into x from t where id = %(scope_id)s", "no place in a read-back"),
        ("select 1 from t where id = %(scope_id)s and n = %(nobody)s", "unknown parameters"),
        ("select 1 from t where id = %(scope_id)s and n like '%s'", "bare %"),
    ],
)
def test_a_query_that_writes_or_escapes_the_scope_is_refused_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], sql: str, why: str
) -> None:
    def never(dsn: str) -> Any:
        raise AssertionError("must not connect")

    data = {**QUERIES, "queries": [{"columns": ["x"], "sql": sql}]}
    assert read_back_sql.main(["--queries", queries_file(tmp_path, data)], ENV, never) == 2
    assert why in capsys.readouterr().err


def test_the_scripts_dry_run_prints_the_queries_and_connects_to_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(dsn: str) -> Any:
        raise AssertionError("must not connect")

    code = read_back_sql.main(["--queries", queries_file(tmp_path), "--dry-run"], ENV, never)
    printed = capsys.readouterr().out
    assert code == 0 and "DRY RUN" in printed and "from patients" in printed
    assert "pw-not-to-print" not in printed and "scope-1" not in printed


async def test_the_script_as_the_batch_runs_it_fails_cleanly_when_it_cannot_read(
    tmp_path: Path,
) -> None:
    """Run as a real command with no connection string: a failed read-back, not a crash."""
    from synthiq_caller.readback import ReadBackContext, ReadBackFailed

    script = str(CALLER / "scripts" / "read_back_sql.py")
    command = CommandReadBack([PY, script, "--queries", queries_file(tmp_path)])
    with pytest.raises(ReadBackFailed, match="exited 2"):
        await command.read(ReadBackContext("a.json", "a", 1, "conv_9", {"name": "Alice W"}))
