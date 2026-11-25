"""Shared test helpers."""

import csv
from pathlib import Path


def mark_checked(directory: Path, *, by: str = "a test") -> None:
    """Write a manifest saying a person has confirmed every clip in ``directory``."""
    with (directory / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "clip",
                "call",
                "category",
                "secs",
                "said",
                "question",
                "review",
                "checked_by_human",
                "provenance",
                "lawful_basis",
                "consent_ref",
            ]
        )
        for clip in sorted(directory.rglob("*.wav")):
            writer.writerow(
                [clip.stem, "", "", "0.25", "", "", "", by, "real", "a test basis", "test-record"]
            )
