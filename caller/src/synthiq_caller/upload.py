"""Send a directory of runs to a Synthiq Voice server, where the dashboard can show it.

    synthiq-caller upload runs/ --api https://voice.example.com \\
        --workspace <workspace id> --target-label "receptionist, test copy"

A batch is a directory: one ``run-NNN.json`` per call, one ``chain-NNN.json`` per
chain and a ``batch.json``. This command packs them into a single document (a
"bundle") and posts it. The server stores every file as it was written, so a
field added to the result format later is not lost by uploading today.

The upload is idempotent. The first upload of a directory writes ``upload.json``
beside the runs with a run id; every later upload of that directory uses the same
id, so sending it twice stores it once. If the directory has changed since (a
regrade, say) the server refuses the upload unless ``--replace`` is given, so a
result somebody has already read is never overwritten by accident.

The API token is read from the environment (``SYNTHIQ_API_TOKEN`` unless
``--token-env`` names another variable) or from ``--env-file``. It is never
printed and never written to the bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx

from synthiq_caller.jsonio import write_json
from synthiq_caller.report import failing_step, summarise

BUNDLE_FORMAT = "synthiq-caller-bundle"
BUNDLE_VERSION = 1
MARKER = "upload.json"
# The server refuses anything larger; saying so here saves a long failed upload.
MAX_BUNDLE_BYTES = 25 * 1024 * 1024
DEFAULT_TOKEN_ENV = "SYNTHIQ_API_TOKEN"  # noqa: S105 - the name of a variable, not a secret


class UploadError(Exception):
    """The upload could not be made or was refused; the message is fit to print."""


def rig_version() -> str:
    try:
        return metadata.version("synthiq-voice-caller")
    except metadata.PackageNotFoundError:
        return "unknown"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")


def _times(runs: list[tuple[Path, dict[str, Any]]]) -> tuple[str | None, str | None]:
    """When the batch started and ended, as near as the files can say.

    A run file is written when its call ends, so the first call's start is that
    file's time less the call's length. Good to a few seconds, which is all a
    list of runs needs.
    """
    if not runs:
        return None, None
    stamped = sorted(
        ((path.stat().st_mtime, payload) for path, payload in runs), key=lambda x: x[0]
    )
    first_time, first = stamped[0]
    length = first.get("record", {}).get("duration_s") if isinstance(first, dict) else None
    started = first_time - (float(length) if isinstance(length, int | float) else 0.0)
    return _iso(started), _iso(stamped[-1][0])


def scenario_set_hash(directory: str | Path) -> str:
    """One hash for a directory of scenario files: their names and exact contents."""
    digest = hashlib.sha256()
    root = Path(directory)
    for path in sorted(p for p in root.iterdir() if p.suffix.lower() in (".json", ".yaml", ".yml")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _marker(
    root: Path, run_id: str | None, started: str | None, ended: str | None
) -> dict[str, Any]:
    """The directory's upload identity: read when it exists, written when it does not.

    Kept in the directory so the run id, and the times taken from the files
    before anything regraded them, are the same on every later upload.
    """
    path = root / MARKER
    stored: dict[str, Any] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        stored = loaded if isinstance(loaded, dict) else {}
    marker = {
        "run_id": run_id or stored.get("run_id") or uuid.uuid4().hex,
        "started_at": stored.get("started_at") or started,
        "ended_at": stored.get("ended_at") or ended,
    }
    if marker != stored:
        write_json(path, marker)
    return marker


def build_bundle(
    directory: str | Path,
    *,
    target_label: str = "",
    run_id: str | None = None,
    scenarios: str | Path | None = None,
    faults: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Everything in a run directory as one document. Places no calls, sends nothing."""
    root = Path(directory)
    files = sorted(root.glob("run-*.json"))
    batch_file = root / "batch.json"
    if not files and not batch_file.exists():
        raise UploadError(f"no runs found in {root}")
    runs = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in files]
    chains = []
    for path in sorted(root.glob("chain-*.json")):
        chain = json.loads(path.read_text(encoding="utf-8"))
        chains.append({**chain, "file": path.name} if isinstance(chain, dict) else {})
    started, ended = _times(runs)
    marker = _marker(root, run_id, started, ended)
    # Calls placed against a scripted stand-in (``batch --leg fake``). Said at the
    # top of the bundle and in the label every list shows, so that a rehearsal
    # cannot be taken for a run against an agent wherever it is displayed.
    rehearsal = any(payload.get("rehearsal") for _, payload in runs)
    if rehearsal and not target_label.upper().startswith("REHEARSAL"):
        target_label = f"REHEARSAL (scripted agent): {target_label}".rstrip(": ")
    return {
        "rehearsal": rehearsal,
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "run_id": marker["run_id"],
        "rig_version": rig_version(),
        "target_label": target_label,
        "scenario_set_hash": scenario_set_hash(scenarios) if scenarios else "",
        "started_at": marker["started_at"],
        "ended_at": marker["ended_at"],
        "batch": (
            json.loads(batch_file.read_text(encoding="utf-8")) if batch_file.exists() else None
        ),
        # This package's own reading of the directory, sent beside the raw files
        # so the server can show it without knowing how it was worked out.
        "summary": asdict(summarise(root)),
        "runs": [
            {
                "file": path.name,
                "last_step": failing_step(payload),
                # A person's judgement of whose fault a failure was, from the same
                # ``--faults`` file the scorecard takes; empty when nobody has said.
                "fault": (faults or {}).get(path.name, ""),
                "payload": payload,
            }
            for path, payload in runs
        ],
        "chains": chains,
    }


def encode(bundle: dict[str, Any]) -> bytes:
    """The bundle as bytes, the same bytes every time for the same bundle.

    The server tells "this exact bundle again" from "this run id with different
    content" by hashing what it receives, so key order must not wander.
    """
    return json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else json.dumps(detail)[:300]


def post_bundle(
    body: bytes,
    *,
    api: str,
    workspace: str,
    token: str,
    replace: bool = False,
    client: httpx.Client | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Post one bundle. Returns the server's answer; raises ``UploadError`` when refused."""
    if len(body) > MAX_BUNDLE_BYTES:
        raise UploadError(
            f"the bundle is {len(body) / 1024 / 1024:.1f} MB; the server takes at most "
            f"{MAX_BUNDLE_BYTES // 1024 // 1024} MB. Upload the batch in parts."
        )
    params = {"workspace_id": workspace}
    if replace:
        params["replace"] = "true"
    owned = client is None
    client = client or httpx.Client(timeout=timeout_s)
    try:
        response = client.post(
            f"{api.rstrip('/')}/api/v1/caller/runs",
            params=params,
            content=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    except httpx.HTTPError as error:
        raise UploadError(f"could not reach {api}: {error.__class__.__name__}") from error
    finally:
        if owned:
            client.close()
    if response.status_code in (200, 201):
        answer = response.json()
        return answer if isinstance(answer, dict) else {}
    if response.status_code == 409:
        raise UploadError(
            f"{_detail(response)}\nThis directory changed since it was last uploaded. "
            "Add --replace to overwrite the stored run."
        )
    if response.status_code in (401, 403):
        raise UploadError("the server refused the token (expired, or not for this server)")
    if response.status_code == 404:
        raise UploadError("the server has no such workspace for this account")
    raise UploadError(f"upload refused ({response.status_code}): {_detail(response)}")


def audio_files(root: Path, runs: list[dict[str, Any]]) -> list[tuple[str, str, Path | None, str]]:
    """The recordings the run files refer to: (run file, track, path or None, why not).

    A call whose file names no audio is not listed: nothing was asked for. A file
    the run names that is not there, or a recording the rig says it could not
    write, is listed with the reason, so that it is said and not skipped.
    """
    found: list[tuple[str, str, Path | None, str]] = []
    base = root.resolve()
    for run_entry in runs:
        audio = (run_entry.get("payload") or {}).get("audio")
        if not isinstance(audio, dict):
            continue
        name = str(run_entry["file"])
        if audio.get("error"):
            found.append((name, "-", None, str(audio["error"])))
            continue
        for track in ("caller", "agent"):
            relative = audio.get(track)
            if not isinstance(relative, str) or not relative:
                continue
            path = (base / relative).resolve()
            if not path.is_relative_to(base):
                found.append((name, track, None, "the path leaves the run directory"))
            elif not path.is_file():
                found.append((name, track, None, f"{relative} is not there"))
            else:
                found.append((name, track, path, ""))
    return found


def post_audio(
    root: Path,
    runs: list[dict[str, Any]],
    *,
    api: str,
    token: str,
    run_pk: str,
    client: httpx.Client | None = None,
    timeout_s: float = 300.0,
) -> tuple[int, list[str]]:
    """Send each recording as its own request. Returns (sent, one line per failure)."""
    sent = 0
    failures: list[str] = []
    owned = client is None
    client = client or httpx.Client(timeout=timeout_s)
    try:
        for name, track, path, why in audio_files(root, runs):
            label = f"{name} {track}"
            if path is None:
                failures.append(f"{label}: {why}")
                continue
            try:
                response = client.put(
                    f"{api.rstrip('/')}/api/v1/caller/runs/{run_pk}/audio",
                    params={"file": name, "track": track},
                    content=path.read_bytes(),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "audio/wav",
                    },
                )
            except (httpx.HTTPError, OSError) as error:
                failures.append(f"{label}: {error.__class__.__name__}")
                continue
            if response.status_code == 200:
                sent += 1
            else:
                failures.append(f"{label}: refused ({response.status_code}) {_detail(response)}")
    finally:
        if owned:
            client.close()
    return sent, failures


def _token(env_name: str, env_file: str | None) -> str:
    value = os.environ.get(env_name, "")
    if not value and env_file:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{env_name}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not value:
        raise UploadError(f"{env_name} is not set (environment or --env-file)")
    return value


def _faults(path: str) -> dict[str, str] | None:
    if not path:
        return None
    from synthiq_caller.scorecard import load_faults

    try:
        return load_faults(path)
    except (OSError, ValueError) as error:
        raise UploadError(f"could not read the faults file: {error}") from error


def run(args: argparse.Namespace) -> int:
    try:
        bundle = build_bundle(
            args.directory,
            target_label=args.target_label,
            run_id=args.run_id,
            scenarios=args.scenarios,
            faults=_faults(args.faults),
        )
        body = encode(bundle)
        if args.bundle_out:
            Path(args.bundle_out).write_bytes(body)
            print(
                f"bundle written: {args.bundle_out} "
                f"({len(body)} bytes, {len(bundle['runs'])} calls)"
            )
            return 0
        if not args.api or not args.workspace:
            raise UploadError(
                "--api and --workspace are required (or use --bundle-out to only pack)"
            )
        token = _token(args.token_env, args.env_file)
        answer = post_bundle(
            body, api=args.api, workspace=args.workspace, token=token, replace=args.replace
        )
    except UploadError as error:
        print(f"upload failed: {error}")
        return 1
    stored = answer.get("run") or {}
    print(
        f"{answer.get('status', 'stored')}: run {bundle['run_id']}, "
        f"{stored.get('attempted', len(bundle['runs']))} calls, "
        f"{stored.get('passed', '?')} passed of {stored.get('graded', '?')} graded"
    )
    if stored.get("id"):
        print(f"dashboard: /dashboard/caller-runs/{stored['id']}")
    if not stored.get("id"):
        wanted = audio_files(Path(args.directory), bundle["runs"])
        if wanted:
            print(
                "audio NOT uploaded: the server did not say which run it stored "
                f"({len(wanted)} files)"
            )
            return 1
        return 0
    sent, failures = post_audio(
        Path(args.directory),
        bundle["runs"],
        api=args.api,
        token=token,
        run_pk=str(stored["id"]),
    )
    if sent or failures:
        print(f"audio: {sent} file(s) uploaded")
    if failures:
        print(f"AUDIO UPLOAD FAILED for {len(failures)} file(s); the run itself was stored:")
        for line in failures:
            print(f"  {line}")
        return 1
    return 0


def register(sub: Any) -> None:
    """Add the ``upload`` command to the command line's subcommands."""
    parser = sub.add_parser("upload", help="send a directory of runs to a Synthiq Voice server")
    parser.add_argument("directory")
    parser.add_argument("--api", default="", help="server address, e.g. https://voice.example.com")
    parser.add_argument("--workspace", default="", help="id of the workspace that will own the run")
    parser.add_argument("--target-label", default="", help="what was tested, in your own words")
    parser.add_argument(
        "--run-id", default=None, help="default: kept in upload.json in the directory"
    )
    parser.add_argument("--scenarios", default=None, help="scenario directory, to record its hash")
    parser.add_argument("--faults", default="", help="file mapping run -> fault class")
    parser.add_argument(
        "--replace", action="store_true", help="overwrite a stored run that changed"
    )
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="variable holding the token")
    parser.add_argument("--env-file", default=None)
    parser.add_argument(
        "--bundle-out", default="", help="write the bundle to a file and send nothing"
    )
    parser.set_defaults(handler=run)
