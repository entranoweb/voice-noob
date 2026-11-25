"""Tests for the caller results API.

Three risks, in the order they would hurt:

1. One customer reading or deleting another's results.
2. A headline pass rate that hides calls: an ungraded call or a call the rig
   never placed must show up as excluded, with its reason, and must never be
   counted as a pass or as a failure.
3. Losing data when the caller's result format moves: unknown outcomes and
   unknown fields have to survive an upload.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditAction
from app.models.audit_event import AuditEvent
from app.models.caller_run import CallerCall, CallerRun
from app.models.user import User
from app.models.workspace import Workspace

if TYPE_CHECKING:
    from httpx import AsyncClient

RUNS = "/api/v1/caller/runs"


def _call(
    scenario: str,
    outcome: str,
    *,
    trace: bool = True,
    refused_tool: bool = False,
    grade_extra: dict[str, Any] | None = None,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One stored run file, shaped as `synthiq_caller.batch` writes it."""
    payload: dict[str, Any] = {
        "scenario_file": f"{scenario}.json",
        "mutates": "none",
        "repeat": 1,
        "record": {
            "scenario": scenario,
            "call_ref": f"conv_{scenario}",
            "ended": "agent ended the call",
            "duration_s": 41.5,
            "timeline": [
                {"t": 0.4, "kind": "agent", "text": "Can I take your date of birth?"},
                {"t": 2.1, "kind": "said", "step": 0, "clips": ["dob.wav"], "seconds": 1.8},
                {"t": 4.2, "kind": "heard", "text": "6th July 1987"},
            ],
            "said": [
                {
                    "t": 2.1,
                    "step_index": 0,
                    "field": "date_of_birth",
                    "intended": "5th July 1987",
                    "clips": ["dob.wav"],
                    "dtmf": "",
                    "seconds": 1.8,
                    "talk_over": False,
                    "answering": "Can I take your date of birth?",
                    "heard": ["6th July 1987"],
                }
            ],
        },
        "trace": (
            {
                "call_ref": f"conv_{scenario}",
                "tool_calls": [
                    {
                        "name": "identify_patient",
                        "arguments": {"dateOfBirth": "1987-07-06"},
                        "result": {"status": "not_found"},
                        "ok": not refused_tool,
                        "step_id": "VERIFY",
                        "t": 5.0,
                        "error": "",
                    }
                ],
                "step_path": ["GREETING", "VERIFY"],
                "termination": "",
            }
            if trace
            else None
        ),
        "trace_error": "",
        "read_back": None,
        "read_back_error": "",
        "hook_error": "",
        "grade": {
            "scenario": scenario,
            "call_ref": f"conv_{scenario}",
            "outcome": outcome,
            "checks": [
                {"what": "the patient was found", "passed": outcome == "passed"},
            ],
            "fields": [
                {
                    "field": "date_of_birth",
                    "said": "5th July 1987",
                    "heard": "6th July 1987",
                    "stored": "1987-07-06",
                    "heard_matches": False,
                    "stored_matches": False,
                    "verdict": "misheard, stored wrongly",
                    "stored_source": "tool call",
                }
            ],
            **(grade_extra or {}),
        },
        **(payload_extra or {}),
    }
    return payload


def _bundle(run_id: str = "run-2026-11-25-a", **overrides: Any) -> dict[str, Any]:
    runs = [
        {"file": "run-001.json", "last_step": "", "payload": _call("existing-patient", "passed")},
        {
            "file": "run-002.json",
            "last_step": "tool identify_patient refused at step VERIFY",
            "payload": _call("misheard-date", "failed", refused_tool=True),
        },
        {
            "file": "run-003.json",
            "last_step": "",
            "payload": _call("no-trace", "ungraded", trace=False),
        },
        {
            "file": "run-004.json",
            "last_step": "",
            # An outcome and a fault class this server has never heard of.
            "payload": _call(
                "reworded-question",
                "rig could not answer",
                grade_extra={"fault_class": "rig", "reason": "no step matched the agent's line"},
            ),
        },
    ]
    bundle: dict[str, Any] = {
        "format": "synthiq-caller-bundle",
        "version": 1,
        "run_id": run_id,
        "rig_version": "0.1.0",
        "target_label": "test agent",
        "scenario_set_hash": "a" * 64,
        "started_at": "2026-11-25T09:00:00+00:00",
        "ended_at": "2026-11-25T09:20:00+00:00",
        "batch": {
            "out": "runs/",
            "ran": 4,
            "blocked": [
                {
                    "scenario_file": "books.json",
                    "scenario": "books-an-appointment",
                    "repeat": 1,
                    "reason": "mutation guard: scenario books, batch allows none",
                }
            ],
            "chains": 0,
        },
        "runs": runs,
        "chains": [],
    }
    bundle.update(overrides)
    return bundle


async def _post(
    client: AsyncClient, workspace_id: Any, bundle: Any, *, replace: bool = False
) -> Any:
    params = {"workspace_id": str(workspace_id)}
    if replace:
        params["replace"] = "true"
    return await client.post(
        RUNS,
        params=params,
        content=bundle if isinstance(bundle, bytes) else json.dumps(bundle).encode(),
        headers={"Content-Type": "application/json"},
    )


async def _someone_elses_run(test_engine: Any) -> tuple[Workspace, CallerRun, CallerCall]:
    """A second user with a workspace, one run and one call, written straight to the database."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        other = User(
            email=f"other-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="not-a-real-hash",  # noqa: S106
            full_name="Other User",
            is_active=True,
            is_superuser=False,
        )
        session.add(other)
        await session.commit()
        await session.refresh(other)
        workspace = Workspace(id=uuid.uuid4(), name="Theirs", user_id=other.id, settings={})
        session.add(workspace)
        await session.commit()
        run = CallerRun(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            user_id=other.id,
            run_id="their-run",
            content_hash="b" * 64,
            payload_version=1,
            attempted=1,
            graded=1,
            passed=1,
            failed=0,
            not_run=0,
            excluded=[],
            chains=[],
            raw={},
        )
        call = CallerCall(
            id=uuid.uuid4(),
            run_pk=run.id,
            workspace_id=workspace.id,
            position=1,
            scenario="their scenario",
            outcome="passed",
            fields=[],
            checks=[],
            timeline=[],
            tool_calls=[],
            raw={},
        )
        session.add(run)
        await session.commit()
        session.add(call)
        await session.commit()
        return workspace, run, call


async def _count(test_engine: Any, model: Any) -> int:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        return int((await session.execute(select(func.count(model.id)))).scalar_one())


class TestUpload:
    @pytest.mark.asyncio
    async def test_counts_come_from_the_calls_and_nothing_is_hidden(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client

        response = await _post(client, test_workspace.id, _bundle())

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "created"
        run = body["run"]
        assert (run["attempted"], run["graded"], run["passed"], run["failed"]) == (4, 2, 1, 1)
        assert run["not_graded"] == 2
        assert run["not_run"] == 1
        # Two calls that prove nothing plus one the rig never placed.
        assert run["excluded_count"] == 3
        assert run["of_graded"]["rate"] == 0.5
        assert run["of_attempted"]["rate"] == 0.25
        assert 0 < run["of_graded"]["interval_low"] < 0.5 < run["of_graded"]["interval_high"] < 1

    @pytest.mark.asyncio
    async def test_the_same_bundle_twice_is_stored_once(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        first = await _post(client, test_workspace.id, _bundle())

        second = await _post(client, test_workspace.id, _bundle())

        assert second.status_code == 200
        assert second.json()["status"] == "unchanged"
        assert second.json()["run"]["id"] == first.json()["run"]["id"]
        assert await _count(test_engine, CallerRun) == 1
        assert await _count(test_engine, CallerCall) == 4

    @pytest.mark.asyncio
    async def test_changed_content_under_a_stored_run_id_is_refused_unless_replacing(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        await _post(client, test_workspace.id, _bundle())
        regraded = _bundle()
        regraded["runs"] = regraded["runs"][:1]

        refused = await _post(client, test_workspace.id, regraded)
        assert refused.status_code == 409
        assert await _count(test_engine, CallerCall) == 4

        replaced = await _post(client, test_workspace.id, regraded, replace=True)
        assert replaced.status_code == 200
        assert replaced.json()["status"] == "replaced"
        assert replaced.json()["run"]["attempted"] == 1
        # The old run's calls went with it.
        assert await _count(test_engine, CallerRun) == 1
        assert await _count(test_engine, CallerCall) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            b"not json at all",
            b'{"format": "something-else", "version": 1, "run_id": "x", "runs": []}',
            b'{"format": "synthiq-caller-bundle", "version": 1, "run_id": "has spaces", "runs": []}',
            b'{"format": "synthiq-caller-bundle", "version": 1, "run_id": "x"}',
            b'{"format": "synthiq-caller-bundle", "version": 1, "run_id": "x", "runs": [{"file": "a"}]}',
            b'{"format": "synthiq-caller-bundle", "version": 1, "run_id": "x", "runs": [], "n": NaN}',
            b"[]",
        ],
    )
    async def test_something_that_is_not_a_bundle_is_rejected_and_stores_nothing(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
        body: bytes,
    ) -> None:
        client, _ = authenticated_test_client

        response = await _post(client, test_workspace.id, body)

        assert response.status_code == 422, response.text
        assert await _count(test_engine, CallerRun) == 0

    @pytest.mark.asyncio
    async def test_an_oversized_bundle_is_refused(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = authenticated_test_client
        monkeypatch.setattr("app.api.caller_runs.MAX_BUNDLE_BYTES", 200)

        response = await _post(client, test_workspace.id, _bundle())

        assert response.status_code == 413
        assert await _count(test_engine, CallerRun) == 0

    @pytest.mark.asyncio
    async def test_an_unsigned_in_upload_is_refused(self, test_client: AsyncClient) -> None:
        response = await _post(test_client, uuid.uuid4(), _bundle())

        assert response.status_code in (401, 403)


class TestReading:
    @pytest.mark.asyncio
    async def test_a_run_lists_every_excluded_call_with_its_reason(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk = (await _post(client, test_workspace.id, _bundle())).json()["run"]["id"]

        body = (await client.get(f"{RUNS}/{run_pk}")).json()

        excluded = {item["scenario"]: item for item in body["excluded"]}
        assert set(excluded) == {"no-trace", "reworded-question", "books-an-appointment"}
        assert "no trace" in excluded["no-trace"]["reason"]
        assert excluded["reworded-question"]["reason"] == "no step matched the agent's line"
        assert excluded["reworded-question"]["outcome"] == "rig could not answer"
        assert excluded["books-an-appointment"]["kind"] == "not run"
        assert "mutation guard" in excluded["books-an-appointment"]["reason"]
        # A call that was placed links to its page; one that never was has nothing to link to.
        assert excluded["no-trace"]["call_id"]
        assert excluded["books-an-appointment"]["call_id"] is None

    @pytest.mark.asyncio
    async def test_a_run_reports_each_scenario_failures_and_fields(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk = (await _post(client, test_workspace.id, _bundle())).json()["run"]["id"]

        body = (await client.get(f"{RUNS}/{run_pk}")).json()

        scenarios = {row["scenario"]: row for row in body["scenarios"]}
        assert scenarios["existing-patient"]["rate"] == 1.0
        assert scenarios["misheard-date"]["rate"] == 0.0
        # Attempted but never graded: no rate, rather than a rate of nought.
        assert scenarios["no-trace"]["graded"] == 0
        assert scenarios["no-trace"]["rate"] is None
        assert scenarios["no-trace"]["interval_low"] is None

        assert [group["fault_class"] for group in body["failures"]] == ["unclassified"]
        failure = body["failures"][0]["calls"][0]
        assert failure["scenario"] == "misheard-date"
        assert failure["last_step"] == "tool identify_patient refused at step VERIFY"
        assert failure["failed_checks"] == ["the patient was found"]

        dob = next(row for row in body["fields"] if row["field"] == "date_of_birth")
        assert (dob["answered"], dob["misheard"], dob["mis_stored"], dob["read_back"]) == (
            4,
            4,
            4,
            0,
        )
        assert [call["position"] for call in body["calls"]] == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_a_run_with_nothing_graded_has_no_pass_rate(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        bundle = _bundle()
        bundle["runs"] = [bundle["runs"][2]]

        run = (await _post(client, test_workspace.id, bundle)).json()["run"]

        assert run["graded"] == 0
        assert run["of_graded"]["rate"] is None
        assert run["of_graded"]["interval_low"] is None
        assert run["of_attempted"]["rate"] == 0.0

    @pytest.mark.asyncio
    async def test_fields_the_server_does_not_know_survive_the_upload(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        bundle = _bundle(
            version=7,
            summary={"of_attempted": {"passed": 1, "total": 5}, "labels_unverified": 2},
        )
        bundle["runs"][0]["payload"]["grade"]["fields"][0]["label_verified"] = False
        run_pk = (await _post(client, test_workspace.id, bundle)).json()["run"]["id"]

        run = (await client.get(f"{RUNS}/{run_pk}")).json()
        call = (await client.get(f"{RUNS}/{run_pk}/calls/{run['calls'][0]['id']}")).json()

        assert run["payload_version"] == 7
        assert run["reported_summary"] == {
            "of_attempted": {"passed": 1, "total": 5},
            "labels_unverified": 2,
        }
        assert call["fields"][0]["label_verified"] is False

    @pytest.mark.asyncio
    async def test_fault_classes_and_tool_outputs_are_passed_through(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        """The uploader's ruling wins; failing that, the fault the rig wrote on the run file."""
        client, _ = authenticated_test_client
        bundle = _bundle()
        bundle["runs"] = [
            {
                "file": "run-001.json",
                "fault": "agent",
                "payload": _call("ruled-by-a-person", "failed", payload_extra={"fault": "rig"}),
            },
            {
                "file": "run-002.json",
                "payload": _call("ruled-by-the-rig", "failed", payload_extra={"fault": "fixture"}),
            },
        ]
        bundle["runs"][0]["payload"]["trace"]["tool_calls"][0]["outputs"] = {"status": "near_match"}
        run_pk = (await _post(client, test_workspace.id, bundle)).json()["run"]["id"]

        run = (await client.get(f"{RUNS}/{run_pk}")).json()
        call = (await client.get(f"{RUNS}/{run_pk}/calls/{run['calls'][0]['id']}")).json()

        assert {group["fault_class"] for group in run["failures"]} == {"agent", "fixture"}
        assert [row["fault_class"] for row in run["calls"]] == ["agent", "fixture"]
        assert call["tool_calls"][0]["outputs"] == {"status": "near_match"}

    @pytest.mark.asyncio
    async def test_a_call_shows_what_was_said_heard_and_stored(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk = (await _post(client, test_workspace.id, _bundle())).json()["run"]["id"]
        run = (await client.get(f"{RUNS}/{run_pk}")).json()
        failed = next(call for call in run["calls"] if call["outcome"] == "failed")
        ungraded = next(call for call in run["calls"] if call["scenario"] == "no-trace")

        call = (await client.get(f"{RUNS}/{run_pk}/calls/{failed['id']}")).json()

        assert call["conversation_id"] == "conv_misheard-date"
        assert [event["kind"] for event in call["timeline"]] == ["agent", "said", "heard"]
        assert call["said"][0]["intended"] == "5th July 1987"
        assert call["tool_calls"][0]["name"] == "identify_patient"
        assert call["tool_calls"][0]["ok"] is False
        assert call["step_path"] == ["GREETING", "VERIFY"]
        row = call["fields"][0]
        assert (row["said"], row["heard"], row["stored"]) == (
            "5th July 1987",
            "6th July 1987",
            "1987-07-06",
        )
        assert row["stored_source"] == "tool call"
        assert call["read_back"] is None
        assert call["excluded_reason"] is None

        other = (await client.get(f"{RUNS}/{run_pk}/calls/{ungraded['id']}")).json()
        assert "no trace" in other["excluded_reason"]
        assert other["tool_calls"] == []

    @pytest.mark.asyncio
    async def test_the_list_is_newest_first_with_a_total(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        for name in ("run-a", "run-b", "run-c"):
            await _post(client, test_workspace.id, _bundle(run_id=name))

        body = (await client.get(RUNS, params={"page_size": 2})).json()

        assert body["total"] == 3
        assert body["total_pages"] == 2
        assert len(body["runs"]) == 2
        assert body["runs"][0]["workspace_name"] == "Test Workspace"

    @pytest.mark.asyncio
    async def test_a_malformed_id_is_rejected_not_a_server_error(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        client, _ = authenticated_test_client

        assert (await client.get(f"{RUNS}/not-a-uuid")).status_code == 422
        assert (await client.get(f"{RUNS}/{uuid.uuid4()}")).status_code == 404


class TestValidityFields:
    """The outcome classes and provenance marks the caller writes since its validity fixes."""

    @pytest.mark.asyncio
    async def test_void_and_ungradeable_calls_are_counted_apart_and_never_graded(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        bundle = _bundle()
        bundle["runs"] = [
            {"file": "run-001.json", "payload": _call("fine", "passed")},
            {
                "file": "run-002.json",
                "payload": _call(
                    "rig-broke",
                    "void",
                    grade_extra={"fault": "rig", "reason": "rig error: socket closed"},
                ),
            },
            {
                "file": "run-003.json",
                "payload": _call(
                    "books-with-no-expectation",
                    "ungradeable",
                    grade_extra={"reason": "the scenario books but states no expected values"},
                ),
            },
        ]
        run_pk = (await _post(client, test_workspace.id, bundle)).json()["run"]["id"]

        run = (await client.get(f"{RUNS}/{run_pk}")).json()

        assert (run["attempted"], run["graded"], run["passed"], run["failed"]) == (3, 1, 1, 0)
        assert run["of_graded"]["rate"] == 1.0
        assert run["of_attempted"]["rate"] == pytest.approx(1 / 3)
        counts = run["outcome_counts"]
        assert (counts["passed"], counts["void"], counts["ungradeable"]) == (1, 1, 1)
        excluded = {item["scenario"]: item for item in run["excluded"]}
        assert excluded["rig-broke"]["outcome"] == "void"
        assert excluded["rig-broke"]["fault"] == "rig"
        assert excluded["rig-broke"]["reason"] == "rig error: socket closed"
        assert "no expected values" in excluded["books-with-no-expectation"]["reason"]
        # A void call is not the agent's failure, so it is not among the failures.
        assert run["failures"] == []

    @pytest.mark.asyncio
    async def test_sent_but_not_confirmed_stored_is_not_counted_as_stored(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        sent = _call("sent-only", "passed")
        sent["grade"]["fields"][0].update(
            heard_matches=True,
            stored_matches=True,
            stored_source="tool call",
            verdict="sent correctly, not confirmed stored",
        )
        read = _call("read-back", "passed")
        read["grade"]["fields"][0].update(
            heard_matches=True, stored_matches=True, stored_source="read back"
        )
        bundle = _bundle()
        bundle["runs"] = [
            {"file": "run-001.json", "payload": sent},
            {"file": "run-002.json", "payload": read},
        ]
        run_pk = (await _post(client, test_workspace.id, bundle)).json()["run"]["id"]

        run = (await client.get(f"{RUNS}/{run_pk}")).json()

        dob = next(row for row in run["fields"] if row["field"] == "date_of_birth")
        assert (dob["answered"], dob["sent_correctly"], dob["stored_correctly"]) == (2, 1, 1)
        assert dob["read_back"] == 1

    @pytest.mark.asyncio
    async def test_synthetic_voices_and_unverified_labels_are_marked_per_call_and_field(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        marked = _call(
            "synthetic-and-unverified",
            "passed",
            payload_extra={
                "synthetic_clips": ["synthetic/dob.wav"],
                "labels_unverified": ["dob.wav"],
            },
        )
        marked["grade"]["fields"][0].update(synthetic=True, labels_unverified=True)
        bundle = _bundle()
        bundle["runs"] = [
            {"file": "run-001.json", "payload": marked},
            {"file": "run-002.json", "payload": _call("plain", "passed")},
        ]
        run_pk = (await _post(client, test_workspace.id, bundle)).json()["run"]["id"]

        run = (await client.get(f"{RUNS}/{run_pk}")).json()
        call = (await client.get(f"{RUNS}/{run_pk}/calls/{run['calls'][0]['id']}")).json()

        assert run["outcome_counts"]["synthetic_calls"] == 1
        assert run["outcome_counts"]["labels_unverified_calls"] == 1
        assert [(row["synthetic"], row["labels_unverified"]) for row in run["calls"]] == [
            (True, True),
            (False, False),
        ]
        dob = next(row for row in run["fields"] if row["field"] == "date_of_birth")
        assert (dob["synthetic"], dob["labels_unverified"]) == (1, 1)
        assert call["synthetic_clips"] == ["synthetic/dob.wav"]
        assert call["labels_unverified"] == ["dob.wav"]
        assert call["fields"][0]["synthetic"] is True


class TestAuditAndErasure:
    @pytest.mark.asyncio
    async def test_upload_and_delete_each_leave_an_audit_event(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, user = authenticated_test_client
        run_pk = (await _post(client, test_workspace.id, _bundle())).json()["run"]["id"]
        await client.delete(f"{RUNS}/{run_pk}")

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            events = (
                (await session.execute(select(AuditEvent).order_by(AuditEvent.created_at)))
                .scalars()
                .all()
            )

        by_action = {event.action: event for event in events}
        assert set(by_action) == {AuditAction.CALLER_RUN_UPLOAD, AuditAction.CALLER_RUN_DELETE}
        upload = by_action[AuditAction.CALLER_RUN_UPLOAD]
        assert upload.actor_user_id == user.id
        assert upload.workspace_id == test_workspace.id
        assert (upload.target_type, upload.target_id) == ("caller_run", run_pk)
        assert upload.details is not None
        assert upload.details["run_id"] == "run-2026-11-25-a"
        assert upload.details["calls"] == 4
        assert by_action[AuditAction.CALLER_RUN_DELETE].target_id == run_pk

    @pytest.mark.asyncio
    async def test_a_refused_upload_leaves_no_audit_event(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client

        await _post(client, test_workspace.id, b"not json")

        assert await _count(test_engine, AuditEvent) == 0

    @pytest.mark.asyncio
    async def test_erasing_an_account_removes_its_caller_runs_and_nobody_elses(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        await _someone_elses_run(test_engine)
        await _post(client, test_workspace.id, _bundle())
        assert await _count(test_engine, CallerRun) == 2
        assert await _count(test_engine, CallerCall) == 5

        response = await client.delete("/api/v1/compliance/data")

        assert response.status_code == 200, response.text
        deleted = response.json()["deleted_counts"]
        assert (deleted["caller_runs"], deleted["caller_calls"]) == (1, 4)
        assert await _count(test_engine, CallerRun) == 1
        assert await _count(test_engine, CallerCall) == 1
        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            left = (await session.execute(select(CallerRun.run_id))).scalars().all()
        assert left == ["their-run"]


class TestDelete:
    @pytest.mark.asyncio
    async def test_deleting_a_run_removes_its_calls(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk = (await _post(client, test_workspace.id, _bundle())).json()["run"]["id"]

        response = await client.delete(f"{RUNS}/{run_pk}")

        assert response.status_code == 204
        assert (await client.get(f"{RUNS}/{run_pk}")).status_code == 404
        assert await _count(test_engine, CallerRun) == 0
        assert await _count(test_engine, CallerCall) == 0


class TestAnotherUsersResults:
    """Every route, tried against a workspace the signed-in user does not own."""

    @pytest.mark.asyncio
    async def test_uploading_into_another_users_workspace_is_refused(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        theirs, _, _ = await _someone_elses_run(test_engine)

        response = await _post(client, theirs.id, _bundle())

        assert response.status_code == 404
        assert await _count(test_engine, CallerRun) == 1  # only the one they already had

    @pytest.mark.asyncio
    async def test_another_users_run_and_call_cannot_be_read(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        _, run, call = await _someone_elses_run(test_engine)

        assert (await client.get(f"{RUNS}/{run.id}")).status_code == 404
        assert (await client.get(f"{RUNS}/{run.id}/calls/{call.id}")).status_code == 404

    @pytest.mark.asyncio
    async def test_another_users_call_cannot_be_read_through_ones_own_run(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        _, _, their_call = await _someone_elses_run(test_engine)
        own_run = (await _post(client, test_workspace.id, _bundle())).json()["run"]["id"]

        response = await client.get(f"{RUNS}/{own_run}/calls/{their_call.id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_another_users_run_cannot_be_deleted(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        _, run, _ = await _someone_elses_run(test_engine)

        response = await client.delete(f"{RUNS}/{run.id}")

        assert response.status_code == 404
        assert await _count(test_engine, CallerRun) == 1
        assert await _count(test_engine, CallerCall) == 1

    @pytest.mark.asyncio
    async def test_another_users_runs_are_not_listed_even_when_asked_for_by_workspace(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        theirs, _, _ = await _someone_elses_run(test_engine)
        await _post(client, test_workspace.id, _bundle())

        everything = (await client.get(RUNS)).json()
        asked = (await client.get(RUNS, params={"workspace_id": str(theirs.id)})).json()

        assert [run["run_id"] for run in everything["runs"]] == ["run-2026-11-25-a"]
        assert everything["total"] == 1
        assert asked["runs"] == []
        assert asked["total"] == 0

    @pytest.mark.asyncio
    async def test_the_same_run_id_in_two_workspaces_does_not_collide(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        await _someone_elses_run(test_engine)

        response = await _post(client, test_workspace.id, _bundle(run_id="their-run"))

        assert response.status_code == 201
        assert await _count(test_engine, CallerRun) == 2
