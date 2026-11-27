"""F21: --shuffle --seed N for batch; the seed is in the manifest; the default order is unchanged."""

import json
from pathlib import Path

import pytest
from test_manifest import LOOKUP, SCRIPT, stored
from test_post_merge_review import write_clips

from synthiq_caller import cli
from synthiq_caller.batch import shuffled

NAMES = [f"{n:02d}-case.json" for n in range(1, 11)]


def test_same_seed_same_order_and_different_seeds_differ() -> None:
    items = [(name, name) for name in NAMES]
    assert shuffled(items, 7) == shuffled(list(reversed(items)), 7)
    assert shuffled(items, 7) != items
    assert shuffled(items, 7) != shuffled(items, 8)
    assert sorted(shuffled(items, 7)) == sorted(items)


def batch(root: Path, *extra: str) -> tuple[int, Path]:
    bank = write_clips(root / "bank")
    (root / "set").mkdir(exist_ok=True)
    for name in NAMES[:3]:
        (root / "set" / name).write_text(json.dumps({**LOOKUP, "name": name}), encoding="utf-8")
    (root / "agent.json").write_text(json.dumps(SCRIPT), encoding="utf-8")
    out = root / "runs"
    arguments = ["batch", str(root / "set"), "--leg", "fake", "--clips", str(bank)]
    arguments += ["--out", str(out), "--settle", "0", "--fake-agent", str(root / "agent.json")]
    return cli.main([*arguments, *extra]), out


def planned(out: Path) -> list[str]:
    entry = stored(out, "manifest.json")["entries"][0]
    return [unit["file"] for unit in entry["plan"]["units"]]


def test_default_order_is_file_name_order_with_no_seed(tmp_path: Path) -> None:
    code, out = batch(tmp_path)
    assert code == 0
    entry = stored(out, "manifest.json")["entries"][0]
    assert planned(out) == NAMES[:3]
    assert entry["seed"] is None and "not shuffled" in entry["order"]


def test_shuffle_with_seed_is_recorded_and_repeatable(tmp_path: Path) -> None:
    code, first = batch(tmp_path / "a", "--shuffle", "--seed", "7")
    assert code == 0
    entry = stored(first, "manifest.json")["entries"][0]
    assert entry["seed"] == 7 and "7" in entry["order"]
    assert planned(first) != NAMES[:3]
    _, second = batch(tmp_path / "b", "--shuffle", "--seed", "7")
    assert planned(second) == planned(first)


def test_shuffle_without_seed_makes_one_and_records_it(tmp_path: Path) -> None:
    code, out = batch(tmp_path, "--shuffle")
    assert code == 0
    assert isinstance(stored(out, "manifest.json")["entries"][0]["seed"], int)


def test_seed_without_shuffle_is_refused_before_anything_runs(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--shuffle"):
        batch(tmp_path, "--seed", "7")
    assert not (tmp_path / "runs").exists()
