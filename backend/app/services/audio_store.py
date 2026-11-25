"""Where the audio of caller calls is kept, and the checks an upload must pass.

``AudioStore`` is the interface; ``LocalDiskAudioStore`` keeps files under one
directory (``CALLER_AUDIO_DIR``). A different backend (object storage) can be
added behind the same four calls without touching the API.

Rules that hold for every store:

* **Names are generated here.** A key is ``<workspace id>/<random hex>.wav``.
  Nothing the client sends (a file name, a path, a header) reaches the file
  system, and a key read back from the database is checked again before use.
* **The content is checked, not the label.** ``inspect_wav`` requires the RIFF
  and WAVE markers and a successful parse by the standard library's ``wave``
  module. The Content-Type header says what the client believes, not what the
  bytes are, and is ignored.
"""

from __future__ import annotations

import io
import os
import re
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.core.config import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

CHUNK = 64 * 1024
# A key as this module makes them: a workspace id, a slash, 32 hex digits.
_KEY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{32}\.wav$"
)
MIN_RATE, MAX_RATE = 8000, 96000


class AudioRejected(ValueError):  # noqa: N818
    """The bytes are not a WAV file this server will keep; the message is fit to show."""


class BadKey(ValueError):  # noqa: N818
    """A storage key that this module did not make."""


@dataclass(frozen=True)
class WavInfo:
    duration_s: float
    sample_rate: int
    channels: int
    sample_width: int
    frames: int


MIN_WAV_BYTES = 44  # a canonical RIFF/WAVE header


def inspect_wav(data: bytes) -> WavInfo:
    """Parse ``data`` as a PCM WAV file or raise ``AudioRejected``."""
    if len(data) < MIN_WAV_BYTES or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioRejected("not a WAV file (no RIFF/WAVE header)")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
    except (wave.Error, EOFError, ValueError, OSError) as error:
        raise AudioRejected(f"not a readable WAV file ({error})") from error
    if channels not in (1, 2):
        raise AudioRejected(f"{channels} channels; only mono or stereo is accepted")
    if width not in (1, 2, 3):
        raise AudioRejected(f"{width * 8}-bit samples are not accepted")
    if not MIN_RATE <= rate <= MAX_RATE:
        raise AudioRejected(f"sample rate {rate} Hz is outside {MIN_RATE}-{MAX_RATE}")
    if frames <= 0:
        raise AudioRejected("the file holds no audio")
    if frames * channels * width > len(data):
        raise AudioRejected("the header claims more audio than the file holds")
    return WavInfo(frames / rate, rate, channels, width, frames)


class RangeNotSatisfiable(ValueError):  # noqa: N818
    """The requested byte range lies outside the file."""


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The single byte range asked for, as inclusive ``(start, end)``; None for the whole file.

    A header this does not understand, or one with several ranges, is answered
    with the whole file, which HTTP allows. A range wholly past the end raises
    ``RangeNotSatisfiable``.
    """
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[6:].strip().partition("-")
    try:
        if first == "":
            length = int(last)
            if length <= 0:
                raise RangeNotSatisfiable(header)
            return max(0, size - length), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or (last and end < start):
        raise RangeNotSatisfiable(header)
    return start, min(end, size - 1)


class AudioStore(Protocol):
    def put(self, workspace_id: uuid.UUID, data: bytes) -> str:
        """Keep ``data``; return the generated key."""
        ...

    def size(self, key: str) -> int: ...

    def read(self, key: str, start: int, end: int) -> Iterator[bytes]:
        """The bytes from ``start`` to ``end`` inclusive, in chunks."""
        ...

    def delete(self, key: str) -> bool:
        """Remove the file. True when it is gone (including when it never was there)."""
        ...


class LocalDiskAudioStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise BadKey("not a key this store made")
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise BadKey("outside the audio directory")
        return path

    def put(self, workspace_id: uuid.UUID, data: bytes) -> str:
        key = f"{workspace_id}/{uuid.uuid4().hex}.wav"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial")
        try:
            with partial.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
        return key

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def read(self, key: str, start: int, end: int) -> Iterator[bytes]:
        path = self._path(key)
        with path.open("rb") as handle:
            handle.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = handle.read(min(CHUNK, left))
                if not chunk:
                    return
                left -= len(chunk)
                yield chunk

    def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink(missing_ok=True)
        except (OSError, BadKey):
            return False
        return True


def get_audio_store() -> AudioStore:
    return LocalDiskAudioStore(settings.CALLER_AUDIO_DIR)
