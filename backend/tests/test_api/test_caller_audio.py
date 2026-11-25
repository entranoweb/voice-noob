"""Tests for the audio of caller calls: upload, serving, deletion, erasure and expiry.

Risks, in the order they would hurt:

1. One customer hearing another's calls.
2. A file that is not audio (or is far too big, or names its own path) reaching
   the disk.
3. Audio outliving its run, its account or its retention period.

Every sound here is a generated tone; no recording of anyone is used.
"""

from __future__ import annotations

import io
import math
import struct
import uuid
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.caller_audio import CallerAudio
from app.models.caller_run import CallerCall, CallerRun
from app.models.workspace import Workspace
from app.services.audio_store import (
    AudioRejected,
    BadKey,
    LocalDiskAudioStore,
    RangeNotSatisfiable,
    get_audio_store,
    inspect_wav,
    parse_range,
)
from app.services.data_protection import cleanup_expired_data
from tests.test_api.test_caller_runs import _bundle, _post, _someone_elses_run

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.models.user import User

RUNS = "/api/v1/caller/runs"


def tone(seconds: float = 0.5, rate: int = 8000, hz: int = 440) -> bytes:
    """A generated sine tone as a mono 16-bit WAV file."""
    frames = b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * hz * i / rate)))
        for i in range(int(seconds * rate))
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def audio_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "audio"
    monkeypatch.setattr(settings, "CALLER_AUDIO_DIR", str(root))
    return root


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _sessions(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _stored_run(client: AsyncClient, workspace: Workspace) -> tuple[str, dict[str, str]]:
    """Upload the sample bundle; return the run's id and the call ids by run file name."""
    response = await _post(client, workspace.id, _bundle())
    assert response.status_code == 201, response.text
    run_pk = response.json()["run"]["id"]
    detail = (await client.get(f"{RUNS}/{run_pk}")).json()
    return run_pk, {f"run-{call['position']:03d}.json": call["id"] for call in detail["calls"]}


async def _put(
    client: AsyncClient,
    run_pk: str,
    data: bytes,
    *,
    file: str = "run-001.json",
    track: str = "agent",
    content_type: str = "audio/wav",
) -> Any:
    return await client.put(
        f"{RUNS}/{run_pk}/audio",
        params={"file": file, "track": track},
        content=data,
        headers={"Content-Type": content_type},
    )


def _audio_url(run_pk: str, call_id: str, track: str) -> str:
    return f"{RUNS}/{run_pk}/calls/{call_id}/audio/{track}"


class TestUploadAndServe:
    @pytest.mark.asyncio
    async def test_both_sides_are_stored_per_workspace_and_served_back_byte_for_byte(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        audio_dir: Path,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, calls = await _stored_run(client, test_workspace)
        agent, caller = tone(0.5, hz=300), tone(0.7, hz=500)

        first = await _put(client, run_pk, agent, track="agent")
        second = await _put(client, run_pk, caller, track="caller")

        assert first.status_code == 200, first.text
        assert first.json()["status"] == "stored"
        assert second.status_code == 200, second.text
        assert first.json()["audio"]["duration_s"] == pytest.approx(0.5)
        files = _files(audio_dir)
        assert len(files) == 2
        # Under the workspace's own folder, with a generated name.
        assert {p.parent.name for p in files} == {str(test_workspace.id)}
        assert all(len(p.stem) == 32 and p.suffix == ".wav" for p in files)

        call_id = calls["run-001.json"]
        served = await client.get(_audio_url(run_pk, call_id, "agent"))
        assert served.status_code == 200
        assert served.content == agent
        assert served.headers["content-type"] == "audio/wav"
        assert served.headers["accept-ranges"] == "bytes"
        detail = (await client.get(f"{RUNS}/{run_pk}/calls/{call_id}")).json()
        assert [(a["track"], a["duration_s"]) for a in detail["audio"]] == [
            ("agent", pytest.approx(0.5)),
            ("caller", pytest.approx(0.7)),
        ]
        other_call = (await client.get(f"{RUNS}/{run_pk}/calls/{calls['run-002.json']}")).json()
        assert other_call["audio"] == []

    @pytest.mark.asyncio
    async def test_sending_a_side_again_replaces_it_and_removes_the_old_file(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        audio_dir: Path,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, calls = await _stored_run(client, test_workspace)
        await _put(client, run_pk, tone(0.3))
        again = await _put(client, run_pk, tone(0.9))

        assert again.json()["status"] == "replaced"
        assert len(_files(audio_dir)) == 1
        served = await client.get(_audio_url(run_pk, calls["run-001.json"], "agent"))
        assert served.content == tone(0.9)

    @pytest.mark.asyncio
    async def test_range_requests_give_partial_content_so_a_player_can_seek(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, calls = await _stored_run(client, test_workspace)
        data = tone(1.0)
        await _put(client, run_pk, data)
        url = _audio_url(run_pk, calls["run-001.json"], "agent")
        size = len(data)

        head = await client.get(url, headers={"Range": "bytes=0-99"})
        assert head.status_code == 206
        assert head.content == data[:100]
        assert head.headers["content-range"] == f"bytes 0-99/{size}"
        assert head.headers["content-length"] == "100"

        tail_from = await client.get(url, headers={"Range": "bytes=8000-"})
        assert tail_from.status_code == 206
        assert tail_from.content == data[8000:]
        assert tail_from.headers["content-range"] == f"bytes 8000-{size - 1}/{size}"

        suffix = await client.get(url, headers={"Range": "bytes=-50"})
        assert suffix.status_code == 206
        assert suffix.content == data[-50:]

        clipped = await client.get(url, headers={"Range": f"bytes=10-{size + 5000}"})
        assert clipped.status_code == 206
        assert clipped.content == data[10:]

        beyond = await client.get(url, headers={"Range": f"bytes={size}-"})
        assert beyond.status_code == 416
        assert beyond.headers["content-range"] == f"bytes */{size}"

    @pytest.mark.asyncio
    async def test_another_user_gets_404_for_upload_and_for_playback(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
        audio_dir: Path,
    ) -> None:
        client, _ = authenticated_test_client
        workspace, run, call = await _someone_elses_run(test_engine)
        store = get_audio_store()
        key = store.put(workspace.id, tone())
        async with _sessions(test_engine)() as session:
            session.add(
                CallerAudio(
                    id=uuid.uuid4(),
                    workspace_id=workspace.id,
                    run_pk=run.id,
                    call_id=call.id,
                    track="agent",
                    storage_key=key,
                    size_bytes=store.size(key),
                    duration_s=0.5,
                    sample_rate=8000,
                    channels=1,
                    sha256="0" * 64,
                )
            )
            await session.commit()

        played = await client.get(_audio_url(str(run.id), str(call.id), "agent"))
        uploaded = await _put(client, str(run.id), tone(), file="run-001.json")
        detail = await client.get(f"{RUNS}/{run.id}/calls/{call.id}")

        assert played.status_code == 404
        assert uploaded.status_code == 404
        assert detail.status_code == 404
        assert played.content != tone(), "the other workspace's sound must not leak"
        assert len(_files(audio_dir)) == 1, "nothing was written by the refused upload"

    @pytest.mark.asyncio
    async def test_a_missing_side_and_a_missing_call_are_404_and_an_unknown_track_is_422(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, calls = await _stored_run(client, test_workspace)

        assert (
            await client.get(_audio_url(run_pk, calls["run-001.json"], "agent"))
        ).status_code == 404
        assert (await _put(client, run_pk, tone(), file="run-099.json")).status_code == 404
        assert (await _put(client, run_pk, tone(), track="both")).status_code == 422
        # A client-chosen name is only ever a lookup key; it cannot reach a path.
        assert (await _put(client, run_pk, tone(), file="../../etc/passwd")).status_code == 404


class TestWhatIsRefused:
    @pytest.mark.asyncio
    async def test_a_file_over_the_cap_is_refused_and_nothing_is_kept(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        audio_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)
        monkeypatch.setattr(settings, "CALLER_AUDIO_MAX_BYTES", 4000)

        response = await _put(client, run_pk, tone(0.5))  # 8044 bytes

        assert response.status_code == 413
        assert _files(audio_dir) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            b"",
            b"this is not audio at all, whatever the label says" * 3,
            b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 100,  # an MP3 tag
            b"RIFF" + b"\x00" * 100,  # half a marker
            b"RIFF\x24\x00\x00\x00WAVEjunk" + b"\x00" * 60,  # both markers, no chunks
        ],
        ids=["empty", "text", "mp3", "riff-only", "markers-only"],
    )
    async def test_bytes_that_are_not_a_wav_file_are_refused_whatever_the_content_type(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        audio_dir: Path,
        body: bytes,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)

        response = await _put(client, run_pk, body, content_type="audio/wav")

        assert response.status_code == 422, response.text
        assert _files(audio_dir) == []

    @pytest.mark.asyncio
    async def test_a_real_wav_is_kept_even_when_it_is_labelled_as_something_else(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)

        response = await _put(client, run_pk, tone(), content_type="text/plain")

        assert response.status_code == 200, "the bytes decide, not the header"

    def test_a_header_that_claims_more_audio_than_the_file_holds_is_refused(self) -> None:
        data = bytearray(tone(0.5))
        truncated = bytes(data[:2000])
        with pytest.raises(AudioRejected, match="more audio"):
            inspect_wav(truncated)

    def test_a_wav_with_no_frames_and_an_odd_sample_rate_are_refused(self) -> None:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
        with pytest.raises(AudioRejected, match="no audio"):
            inspect_wav(buffer.getvalue())
        with pytest.raises(AudioRejected, match="sample rate"):
            inspect_wav(tone(0.1, rate=1000))


class TestStoreAndRange:
    def test_a_key_the_store_did_not_make_cannot_reach_a_file(self, tmp_path: Path) -> None:
        store = LocalDiskAudioStore(tmp_path / "root")
        outside = tmp_path / "secret.wav"
        outside.write_bytes(b"x")
        for key in (
            "../secret.wav",
            "../../etc/passwd",
            f"{uuid.uuid4()}/../../secret.wav",
            "/etc/passwd",
            f"{uuid.uuid4()}/abc.wav",
            "",
        ):
            with pytest.raises(BadKey):
                store.size(key)
        assert store.delete("../secret.wav") is False
        assert outside.exists()

    def test_put_makes_the_name_and_delete_of_a_missing_file_is_not_a_failure(
        self, tmp_path: Path
    ) -> None:
        store = LocalDiskAudioStore(tmp_path / "root")
        workspace = uuid.uuid4()
        key = store.put(workspace, tone())
        assert key.startswith(f"{workspace}/")
        assert key.endswith(".wav")
        assert store.delete(key) is True
        assert store.delete(key) is True

    def test_parse_range_understands_the_forms_a_browser_sends(self) -> None:
        assert parse_range(None, 1000) is None
        assert parse_range("bytes=0-", 1000) == (0, 999)
        assert parse_range("bytes=100-199", 1000) == (100, 199)
        assert parse_range("bytes=-100", 1000) == (900, 999)
        assert parse_range("bytes=0-1,5-9", 1000) is None  # several ranges: whole file
        assert parse_range("items=0-5", 1000) is None
        with pytest.raises(RangeNotSatisfiable):
            parse_range("bytes=1000-", 1000)
        with pytest.raises(RangeNotSatisfiable):
            parse_range("bytes=50-10", 1000)


async def _put_audio_row(
    engine: Any, run: CallerRun, call: CallerCall, *, age_days: float, track: str = "agent"
) -> tuple[uuid.UUID, str]:
    store = get_audio_store()
    key = store.put(run.workspace_id, tone(0.2))
    row_id = uuid.uuid4()
    async with _sessions(engine)() as session:
        session.add(
            CallerAudio(
                id=row_id,
                workspace_id=run.workspace_id,
                run_pk=run.id,
                call_id=call.id,
                track=track,
                storage_key=key,
                size_bytes=store.size(key),
                duration_s=0.2,
                sample_rate=8000,
                channels=1,
                sha256="0" * 64,
                created_at=datetime.now(UTC) - timedelta(days=age_days),
            )
        )
        await session.commit()
    return row_id, key


class TestDeletion:
    @pytest.mark.asyncio
    async def test_deleting_a_run_deletes_its_audio_files_and_rows_and_only_theirs(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
        audio_dir: Path,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)
        await _put(client, run_pk, tone(), track="agent")
        await _put(client, run_pk, tone(), track="caller")
        second = await _post(client, test_workspace.id, _bundle(run_id="another-run"))
        second_pk = second.json()["run"]["id"]
        await _put(client, second_pk, tone(0.2), track="agent")
        assert len(_files(audio_dir)) == 3

        response = await client.delete(f"{RUNS}/{run_pk}")

        assert response.status_code == 204
        assert len(_files(audio_dir)) == 1, "only the other run's file is left"
        async with _sessions(test_engine)() as session:
            rows = (await session.execute(sa.select(CallerAudio.run_pk))).scalars().all()
        assert [str(r) for r in rows] == [second_pk]

    @pytest.mark.asyncio
    async def test_a_run_is_kept_and_the_failure_said_when_a_file_cannot_be_deleted(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)
        await _put(client, run_pk, tone())
        monkeypatch.setattr(LocalDiskAudioStore, "delete", lambda _self, _key: False)

        response = await client.delete(f"{RUNS}/{run_pk}")

        assert response.status_code == 500
        assert "could not be deleted" in response.json()["detail"]
        assert (await client.get(f"{RUNS}/{run_pk}")).status_code == 200

    @pytest.mark.asyncio
    async def test_replacing_a_run_removes_the_audio_of_the_calls_it_replaces(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        audio_dir: Path,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)
        await _put(client, run_pk, tone())

        changed = _bundle(target_label="changed")
        response = await _post(client, test_workspace.id, changed, replace=True)

        assert response.status_code == 200
        assert response.json()["status"] == "replaced"
        assert _files(audio_dir) == []

    @pytest.mark.asyncio
    async def test_erasure_deletes_the_accounts_audio_and_not_another_accounts(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
        audio_dir: Path,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)
        await _put(client, run_pk, tone(), track="agent")
        await _put(client, run_pk, tone(), track="caller")
        _workspace, other_run, other_call = await _someone_elses_run(test_engine)
        _other_id, other_key = await _put_audio_row(test_engine, other_run, other_call, age_days=0)

        response = await client.delete("/api/v1/compliance/data")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["deleted_counts"]["caller_audio"] == 2
        assert body["complete"] is True
        assert [p.name for p in _files(audio_dir)] == [Path(other_key).name]
        async with _sessions(test_engine)() as session:
            left = (await session.execute(sa.select(CallerAudio.run_pk))).scalars().all()
        assert left == [other_run.id]

    @pytest.mark.asyncio
    async def test_erasure_is_not_called_complete_when_an_audio_file_will_not_go(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)
        await _put(client, run_pk, tone())
        monkeypatch.setattr(LocalDiskAudioStore, "delete", lambda _self, _key: False)

        response = await client.delete("/api/v1/compliance/data")

        body = response.json()
        assert body["complete"] is False
        assert [item["provider"] for item in body["not_erased_here"]] == ["audio_store"]
        assert body["deleted_counts"]["caller_audio_left"] == 1

    @pytest.mark.asyncio
    async def test_the_export_lists_the_audio_with_when_it_will_be_deleted(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        run_pk, _calls = await _stored_run(client, test_workspace)
        await _put(client, run_pk, tone(0.5))
        _w, other_run, other_call = await _someone_elses_run(test_engine)
        await _put_audio_row(test_engine, other_run, other_call, age_days=0)

        response = await client.get("/api/v1/compliance/export")

        assert response.status_code == 200, response.text
        listed = response.json()["caller_audio"]
        assert [(a["track"], a["run_id"]) for a in listed] == [("agent", run_pk)]
        assert listed[0]["duration_s"] == pytest.approx(0.5)
        stored = datetime.fromisoformat(listed[0]["stored_at"])
        deleted_after = datetime.fromisoformat(listed[0]["deleted_after"])
        assert deleted_after - stored == timedelta(days=30)
        assert "storage_key" not in listed[0]


class TestRetention:
    @pytest.mark.asyncio
    async def test_expiry_removes_only_audio_older_than_its_workspaces_period(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        test_engine: Any,
        audio_dir: Path,
    ) -> None:
        client, user = authenticated_test_client
        run_pk, calls = await _stored_run(client, test_workspace)
        async with _sessions(test_engine)() as session:
            run = (
                await session.execute(sa.select(CallerRun).where(CallerRun.id == uuid.UUID(run_pk)))
            ).scalar_one()
            call_rows = (
                (await session.execute(sa.select(CallerCall).where(CallerCall.run_pk == run.id)))
                .scalars()
                .all()
            )
        by_file = {c.file: c for c in call_rows}
        old_id, old_key = await _put_audio_row(
            test_engine, run, by_file["run-001.json"], age_days=31
        )
        young_id, young_key = await _put_audio_row(
            test_engine, run, by_file["run-002.json"], age_days=29
        )
        # A workspace that keeps audio for 90 days keeps 45-day-old audio.
        async with _sessions(test_engine)() as session:
            longer = Workspace(
                id=uuid.uuid4(),
                name="Longer",
                user_id=user.id,
                settings={},
                audio_retention_days=90,
            )
            session.add(longer)
            await session.commit()
            longer_run = CallerRun(
                id=uuid.uuid4(),
                workspace_id=longer.id,
                user_id=user.id,
                run_id="longer",
                content_hash="c" * 64,
                payload_version=1,
                attempted=1,
                graded=0,
                passed=0,
                failed=0,
                not_run=0,
                excluded=[],
                chains=[],
                raw={},
            )
            session.add(longer_run)
            await session.commit()
            longer_call = CallerCall(
                id=uuid.uuid4(),
                run_pk=longer_run.id,
                workspace_id=longer.id,
                position=1,
                file="run-001.json",
                scenario="s",
                outcome="passed",
                fields=[],
                checks=[],
                timeline=[],
                tool_calls=[],
                raw={},
            )
            session.add(longer_call)
            await session.commit()
        kept_id, kept_key = await _put_audio_row(test_engine, longer_run, longer_call, age_days=45)
        assert len(_files(audio_dir)) == 3
        del calls

        async with _sessions(test_engine)() as session:
            dry = await cleanup_expired_data(session, user_id=user.id, dry_run=True)
        assert dry["caller_audio"] == 1
        assert len(_files(audio_dir)) == 3, "a dry run deletes nothing"

        async with _sessions(test_engine)() as session:
            counts = await cleanup_expired_data(session, user_id=user.id)
            await session.commit()

        assert counts["caller_audio"] == 1
        remaining = {p.name for p in _files(audio_dir)}
        assert remaining == {Path(young_key).name, Path(kept_key).name}
        assert Path(old_key).name not in remaining
        async with _sessions(test_engine)() as session:
            ids = set((await session.execute(sa.select(CallerAudio.id))).scalars().all())
        assert ids == {young_id, kept_id}
        assert old_id not in ids

    @pytest.mark.asyncio
    async def test_the_period_is_a_workspace_setting_with_a_default_of_30_and_limits(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        url = f"/api/v1/workspaces/{test_workspace.id}"

        before = await client.get(url)
        assert before.json()["audio_retention_days"] == 30

        changed = await client.put(url, json={"audio_retention_days": 90})
        assert changed.status_code == 200, changed.text
        assert changed.json()["audio_retention_days"] == 90
        assert (await client.put(url, json={"audio_retention_days": 0})).status_code == 422
        assert (await client.put(url, json={"audio_retention_days": 99999})).status_code == 422
        unchanged = await client.put(url, json={"audio_retention_days": None, "name": "Renamed"})
        assert unchanged.status_code == 200
        assert unchanged.json()["audio_retention_days"] == 90
