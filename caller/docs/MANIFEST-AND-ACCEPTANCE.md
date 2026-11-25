# The run manifest and the acceptance report

Two things a result needs before it can be handed to anyone: a record of exactly what produced it, and a dated statement of what was tested and what passed.

## The run manifest

Every `batch` and `run` writes an entry into `manifest.json` in its run directory before it places a call, and closes the entry when it ends. The entry records:

| Part | What is kept |
|---|---|
| Rig | Package version, the commit it ran from, and whether the rig's source had changes no commit holds |
| Scenarios | The SHA-256 of every scenario and chain file the batch was given, and one hash for the set |
| Clips | For each clip bank directory: the hash of its `manifest.csv`, the hash of its `overrides.json`, and how many clips it holds. One hash over the audio of every clip the scenarios name |
| Target | The label and version you state (`--target-label`, `--target-version`), the agent id, the leg, the trace source, and the target profile's file name and hash |
| Run | Start and end times, the command line, repeats, the mutation level allowed, whether it was a rehearsal |
| Order | Scenarios run in file name order. Nothing is shuffled, so the seed is `null` |

A directory can hold more than one batch. Each stored run carries the id of the entry it was made under (`"batch": "b001"`).

Text files are hashed with line endings normalised, so a scenario has the same hash on Windows and Linux. Audio is hashed as it is.

The rig does not ask any platform for the target's version. What the manifest holds is what the operator stated, and reports say so. When the rig runs from an installed package with no checkout around it, set `SYNTHIQ_RIG_COMMIT`; otherwise the commit is recorded as `unknown`.

The command line is stored as typed. Secrets are never on it: they are named by environment variable.

### Runs made against different versions of a scenario

A pass against last week's wording of a scenario and a fail against this week's are not two results of one test. `regrade` and `acceptance` refuse a set of runs in which the same scenario file answers to more than one hash.

- `regrade` grades stored calls against the scenarios as they are now, so it records an entry of its own. From then on the calls it covered answer to the regrade's hashes. A batch placed later answers to its own.
- A run with no manifest entry answers to `unrecorded`. A directory made entirely before manifests existed is consistent with itself; mixed with recorded runs it is refused.
- `--allow-mixed-scenarios` goes on regardless. `regrade` then brings every stored call to one version. `acceptance` prints the mix on the report.
