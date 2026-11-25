"""Compare the migrated database with the models and name each difference.

The tests build their schema from the models, never from the migrations, so a
model change with no migration passes every test and then breaks the next
deployment. `scripts/check_migration_drift.py` runs this in CI straight after
`alembic upgrade head`.

`alembic check` does the same comparison but can only say "something differs".
The first run of that on this repository found dozens of differences that
predate the check, so a plain pass/fail gate would have been red from day one
and switched off the day after. Instead every difference gets a stable one-line
key, the known ones are listed in `migrations/drift-baseline.txt`, and the gate
fails on two things only:

- a difference that is not in the baseline (new drift: write a migration);
- a baseline line that no longer matches anything (fixed drift: delete the
  line, so it cannot quietly come back).

Column comments are not compared. A comment is documentation inside the
database, it changes no behaviour, and the models carry several dozen that no
migration ever wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

IGNORED_OPERATIONS = frozenset({"modify_comment", "add_table_comment", "remove_table_comment"})


def _columns(columns: Iterable[Any]) -> str:
    return ",".join(getattr(column, "name", str(column)) for column in columns)


def _key_for(diff: Sequence[Any]) -> str | None:  # noqa: PLR0911
    """One stable line for one difference, or None when it is not compared."""
    operation = str(diff[0])
    if operation in IGNORED_OPERATIONS:
        return None

    if operation in {"add_table", "remove_table"}:
        return f"{operation} {diff[1].name}"

    if operation in {"add_column", "remove_column"}:
        return f"{operation} {diff[2]}.{diff[3].name}"

    if operation in {"add_index", "remove_index"}:
        index = diff[1]
        unique = " unique" if index.unique else ""
        return f"{operation} {index.name} on {index.table.name}({_columns(index.columns)}){unique}"

    if operation in {"add_constraint", "remove_constraint"}:
        constraint = diff[1]
        return (
            f"{operation} {constraint.name} on "
            f"{constraint.table.name}({_columns(constraint.columns)})"
        )

    if operation in {"add_fk", "remove_fk"}:
        fk = diff[1]
        target = ",".join(element.target_fullname for element in fk.elements)
        return (
            f"{operation} {fk.parent.name}({_columns(fk.columns)}) -> {target} "
            f"ondelete={fk.ondelete}"
        )

    if operation in {"modify_type", "modify_nullable", "modify_default"}:
        # (operation, schema, table, column, existing keywords, in database, in models)
        return f"{operation} {diff[2]}.{diff[3]}: {diff[5]!r} -> {diff[6]!r}"

    # An operation this module has not met. Keep it visible rather than drop it:
    # an unrecognised difference must fail the gate, not slip past it.
    return f"{operation} {' '.join(str(part) for part in diff[1:4])}"


def drift_keys(diffs: Iterable[Any]) -> list[str]:
    """Flatten Alembic's differences into sorted, de-duplicated keys.

    Alembic returns a tuple per difference, except column changes, which arrive
    as a list of tuples for one column.
    """
    keys: set[str] = set()
    for diff in diffs:
        group = diff if isinstance(diff, list) else [diff]
        for item in group:
            key = _key_for(item)
            if key is not None:
                keys.add(key)
    return sorted(keys)


def parse_baseline(text: str) -> list[str]:
    """Baseline lines, without blank lines and `#` comments."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@dataclass(frozen=True)
class DriftReport:
    """What the gate found, split by what has to be done about it."""

    new: list[str]
    fixed: list[str]
    known: list[str]

    @property
    def ok(self) -> bool:
        return not self.new and not self.fixed


def compare_with_baseline(found: Iterable[str], baseline: Iterable[str]) -> DriftReport:
    found_set, baseline_set = set(found), set(baseline)
    return DriftReport(
        new=sorted(found_set - baseline_set),
        fixed=sorted(baseline_set - found_set),
        known=sorted(found_set & baseline_set),
    )
