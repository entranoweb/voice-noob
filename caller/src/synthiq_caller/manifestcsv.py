"""Read a clip bank's ``manifest.csv`` without ever losing a value a person typed.

``csv.DictReader`` keeps only the last copy of a column that appears twice, so a
person who filled in the first copy was silently ignored. ``ingest`` used to write
``provenance``, ``lawful_basis`` and ``consent_ref`` twice. The rule here:

* a column that appears once is read as it is;
* a column that appears more than once is merged when the copies do not conflict
  (at most one distinct non-empty value: a filled copy beats an empty one);
* copies that disagree raise ``ManifestError``. Nothing is guessed, because a guess
  at a consent field could make a clip playable that a person never cleared.
"""

from __future__ import annotations

import csv
from pathlib import Path


class ManifestError(ValueError):
    """The manifest cannot be read without ignoring something a person wrote."""


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """The manifest's column names (each once) and its rows, copies of a column merged."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return [], []
        names = [h.strip() for h in header]
        columns = list(dict.fromkeys(names))
        rows: list[dict[str, str]] = []
        for cells in reader:
            if not cells:
                continue  # a blank line, as DictReader skips it
            row: dict[str, str] = {}
            for name in columns:
                held = [cells[i] for i, n in enumerate(names) if n == name and i < len(cells)]
                filled = list(dict.fromkeys(c for c in held if c.strip()))
                if len({c.strip() for c in filled}) > 1:
                    raise ManifestError(
                        f"{path}, line {reader.line_num}: the column {name!r} appears "
                        f"{names.count(name)} times with different values; nothing was "
                        "read. Keep one copy of the column, holding the value you mean."
                    )
                row[name] = filled[0] if filled else ""
            rows.append(row)
    return columns, rows
