"""The upload command: what it packs, that packing is repeatable, and what it does when refused."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from synthiq_caller.cli import main
from synthiq_caller.upload import (
    MARKER,
    UploadError,
    build_bundle,
    encode,
    post_bundle,
    scenario_set_hash,
)


def _run(scenario: str, outcome: str) -> dict[str, Any]:
    return {
        "scenario_file": f"{scenario}.json",
        "mutates": "none",
        "repeat": 1,
        "record": {
            "scenario": scenario,
            "call_ref": f"conv_{scenario}",
            "ended": "agent ended the call",
            "duration_s": 30.0,
            "timeline": [{"t": 0.5, "kind": "agent", "text": "Can I take your name?"}],
            "said": [],
        },
        "trace": {
            "call_ref": f"conv_{scenario}",
            "tool_calls": [
                {
                    "name": "lookup",
                    "arguments": {},
                    "result": None,
                    "ok": False,
                    "step_id": "VERIFY",
                }
            ],
            "step_path": ["VERIFY"],
            "termination": "",
        },
        "trace_error": "",
        "grade": {
            "scenario": scenario,
            "call_ref": f"conv_{scenario}",
            "outcome": outcome,
            "checks": [{"what": "the patient was found", "passed": outcome == "passed"}],
            "fields": [],
        },
    }


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    out = tmp_path / "runs"
    out.mkdir()
    (out / "run-001.json").write_text(json.dumps(_run("first", "passed")), encoding="utf-8")
    (out / "run-002.json").write_text(json.dumps(_run("second", "failed")), encoding="utf-8")
    (out / "chain-001.json").write_text(
        json.dumps({"chain": "book then cancel", "outcome": "passed", "calls": []}),
        encoding="utf-8",
    )
    (out / "batch.json").write_text(
        json.dumps(
            {
                "out": str(out),
                "ran": 2,
                "chains": 1,
                "blocked": [
                    {
                        "scenario_file": "books.json",
                        "scenario": "books",
                        "repeat": 1,
                        "reason": "mutation guard",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return out


def test_the_bundle_holds_every_file_as_written(runs: Path) -> None:
    bundle = build_bundle(runs, target_label="test agent")

    assert bundle["format"] == "synthiq-caller-bundle"
    assert bundle["target_label"] == "test agent"
    assert [entry["file"] for entry in bundle["runs"]] == ["run-001.json", "run-002.json"]
    assert bundle["runs"][1]["payload"] == _run("second", "failed")
    assert bundle["runs"][1]["last_step"] == "tool lookup refused at step VERIFY"
    assert bundle["chains"][0]["file"] == "chain-001.json"
    assert bundle["batch"]["blocked"][0]["reason"] == "mutation guard"
    assert (bundle["summary"]["passed"], bundle["summary"]["failed"]) == (1, 1)
    assert bundle["started_at"] and bundle["ended_at"]


def test_fault_classes_and_tool_outputs_travel_with_the_bundle(runs: Path) -> None:
    """A person's fault ruling rides beside the run; what a tool reported back is left as written."""
    payload = _run("second", "failed")
    payload["trace"]["tool_calls"][0]["outputs"] = {"status": "near_match"}
    (runs / "run-002.json").write_text(json.dumps(payload), encoding="utf-8")

    bundle = build_bundle(runs, faults={"run-002.json": "agent"})

    assert [entry["fault"] for entry in bundle["runs"]] == ["", "agent"]
    sent = bundle["runs"][1]["payload"]["trace"]["tool_calls"][0]
    assert sent["outputs"] == {"status": "near_match"}


def test_packing_the_same_directory_twice_gives_the_same_bytes(runs: Path) -> None:
    """The server knows "this bundle again" by its hash, so a second pack must not differ."""
    first = encode(build_bundle(runs, target_label="test agent"))
    marker = json.loads((runs / MARKER).read_text(encoding="utf-8"))

    second = encode(build_bundle(runs, target_label="test agent"))

    assert first == second
    assert json.loads(first)["run_id"] == marker["run_id"]


def test_a_regrade_keeps_the_run_id_but_changes_the_content(runs: Path) -> None:
    before = build_bundle(runs)
    (runs / "run-002.json").write_text(json.dumps(_run("second", "passed")), encoding="utf-8")

    after = build_bundle(runs)

    assert after["run_id"] == before["run_id"]
    assert (after["started_at"], after["ended_at"]) == (before["started_at"], before["ended_at"])
    assert encode(after) != encode(before)


def test_an_empty_directory_is_an_error_not_an_empty_upload(tmp_path: Path) -> None:
    with pytest.raises(UploadError, match="no runs found"):
        build_bundle(tmp_path)


def test_the_scenario_hash_follows_the_contents(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    first = scenario_set_hash(tmp_path)
    (tmp_path / "a.json").write_text('{"name": "changed"}', encoding="utf-8")

    assert scenario_set_hash(tmp_path) != first


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_post_carries_the_token_workspace_and_exact_bytes(runs: Path) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(201, json={"status": "created", "run": {"id": "abc"}})

    body = encode(build_bundle(runs))
    answer = post_bundle(
        body,
        api="https://voice.test/",
        workspace="ws-1",
        token="secret-token",  # noqa: S106
        replace=True,
        client=_client(handler),
    )

    assert answer["status"] == "created"
    assert seen["url"] == "https://voice.test/api/v1/caller/runs?workspace_id=ws-1&replace=true"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["body"] == body


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (409, "--replace"),
        (401, "refused the token"),
        (404, "no such workspace"),
        (422, "not a caller bundle"),
    ],
)
def test_a_refusal_says_what_to_do_and_never_shows_the_token(status: int, said: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "not a caller bundle"})

    with pytest.raises(UploadError) as raised:
        post_bundle(
            b"{}",
            api="https://voice.test",
            workspace="ws-1",
            token="secret-token",  # noqa: S106
            client=_client(handler),
        )

    assert said in str(raised.value)
    assert "secret-token" not in str(raised.value)


def test_an_unreachable_server_is_reported_without_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(UploadError) as raised:
        post_bundle(
            b"{}",
            api="https://voice.test",
            workspace="ws-1",
            token="secret-token",  # noqa: S106
            client=_client(handler),
        )

    assert "could not reach" in str(raised.value)
    assert "secret-token" not in str(raised.value)


def test_the_command_can_pack_without_sending(
    runs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "bundle.json"

    code = main(["upload", str(runs), "--bundle-out", str(out)])

    assert code == 0
    assert json.loads(out.read_bytes())["format"] == "synthiq-caller-bundle"
    assert "2 calls" in capsys.readouterr().out


def test_the_command_fails_plainly_without_a_token(
    runs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SYNTHIQ_API_TOKEN", raising=False)

    code = main(["upload", str(runs), "--api", "https://voice.test", "--workspace", "ws-1"])

    assert code == 1
    assert "SYNTHIQ_API_TOKEN is not set" in capsys.readouterr().out
