"""What a clip is called: its file name without an audio ending, and nothing else.

``Path(x).stem`` strips any ending, so ``a.v2`` would become ``a`` and two clips
would share one id. Only a known audio ending is taken off.
"""

from __future__ import annotations

from pathlib import Path

AUDIO_SUFFIXES = (".wav", ".mp3", ".flac")


def clip_id(name: str) -> str:
    """``opener.wav`` and ``takes/opener.wav`` are ``opener``; ``a.v2`` stays ``a.v2``."""
    base = Path(str(name)).name
    for suffix in AUDIO_SUFFIXES:
        if base.lower().endswith(suffix):
            return base[: -len(suffix)]
    return base
