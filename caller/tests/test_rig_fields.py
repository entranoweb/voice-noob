"""forget.RIG_FIELDS must list every top-level key a writer puts in a result file.

``forget --erase-altered`` clears every field NOT in that list from an altered file, so a key
a writer adds and the list does not know would be silently destroyed. This produces a result
from each writer and fails when one holds a key the list does not.
"""

import json
from pathlib import Path

from test_batch_and_report import FAST, agent, scenario
from test_clip_consent import FULL, write_bank
from test_phone_leg import scenario_file, write_clip
from test_reset_chains_readback import BOOKED, CANCELLED, agents, chain_data

from synthiq_caller.batch import load_chains, regrade, regrade_chains, run_batch
from synthiq_caller.clips import ClipBank
from synthiq_caller.forget import RIG_FIELDS
from synthiq_caller.jsonio import write_json
from synthiq_caller.phone_server import main as phone_main


def keys(directory: Path, pattern: str) -> dict[str, set[str]]:
    found = {
        p.name: set(json.loads(p.read_text(encoding="utf-8"))) for p in directory.glob(pattern)
    }
    assert found, f"no {pattern} written: the writer did not run"
    return found


def unknown(written: dict[str, set[str]]) -> dict[str, list[str]]:
    return {name: sorted(k - RIG_FIELDS) for name, k in written.items() if k - RIG_FIELDS}


async def test_every_key_a_writer_puts_in_a_result_is_a_rig_field(tmp_path: Path) -> None:
    (tmp_path / "bank" / "days").mkdir(parents=True)
    bank = write_bank(
        tmp_path / "bank",
        {"opener": FULL, "dob": FULL, "cancel": FULL, "days/wednesday-3rd": FULL},
    )
    # a batch run and a chain, on the fake leg
    out = tmp_path / "out"
    chains = tmp_path / "chains"
    chains.mkdir()
    (chains / "chain.json").write_text(json.dumps(chain_data()), encoding="utf-8")
    await run_batch(
        [("lookup.json", scenario("lookup"))],
        agents([[BOOKED], [CANCELLED]]),
        ClipBank(bank),
        out,
        chains=load_chains(chains),
        allow_mutating="all",
        turn_settings=FAST,
    )
    # a plain run through the other agent factory too (a trace and no trace)
    plain = tmp_path / "plain"
    await run_batch(
        [("lookup.json", scenario("lookup"))], agent, ClipBank(bank), plain, turn_settings=FAST
    )
    written = {**keys(out, "run-*.json"), **keys(out, "chain-*.json")}
    written |= {f"plain/{n}": k for n, k in keys(plain, "run-*.json").items()}

    # regrade rewrites a file whose stored grade is wrong: make them wrong, so it writes, and
    # whatever it adds counts too
    for directory in (out, plain):
        for file in directory.glob("run-*.json"):
            data = json.loads(file.read_text(encoding="utf-8"))
            data["grade"]["outcome"] = (
                "failed" if data["grade"]["outcome"] != "failed" else "passed"
            )
            write_json(file, data)
    for file in out.glob("chain-*.json"):
        data = json.loads(file.read_text(encoding="utf-8"))
        data["outcome"] = "failed"
        write_json(file, data)
    regrade(out, [("lookup.json", scenario("lookup"))])
    regrade_chains(out, load_chains(chains))
    regrade(plain, [("lookup.json", scenario("lookup"))])
    after = {**keys(out, "run-*.json"), **keys(out, "chain-*.json")}
    after |= {f"plain/{n}": k for n, k in keys(plain, "run-*.json").items()}

    assert unknown({**written, **after}) == {}, (
        "a writer puts a top-level key in a result file that forget.RIG_FIELDS does not list: "
        "add it there, or forget --erase-altered would clear it"
    )


def test_every_key_a_phone_run_holds_is_a_rig_field(tmp_path: Path) -> None:
    # a phone run (the dry-run fixture writes the same file a real call does)
    write_clip(tmp_path, "opener.wav", 0.4)
    path = scenario_file(tmp_path)
    flags = ["--allow-unknown-provenance", "--allow-missing-consent", "--dry-run"]
    code = phone_main(
        [
            "--scenario",
            str(path),
            "--clips",
            str(tmp_path),
            "--out",
            str(tmp_path / "phone"),
            *flags,
        ],
        env={},
    )
    assert code == 0
    phone = keys(tmp_path / "phone", "phone-run-*.json")

    assert unknown(phone) == {}, (
        "a writer puts a top-level key in a phone run that forget.RIG_FIELDS does not list: "
        "add it there, or forget --erase-altered would clear it"
    )
