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

## The acceptance report

```
synthiq-caller acceptance runs/monday runs/tuesday --claim 0.95 --out report.html
```

One self-contained page, with its data beside it as `report.json`. It has no arithmetic of its own: the counts are the `report` command's, the verdict is the rule behind the `gate` command, and what produced the runs is read from each directory's manifest.

### What is on the page

- The target's label and version, the rig's version and commit, the scenario set hash and each scenario file's hash, the clip bank's identity, and when the calls were placed.
- Calls attempted, not run, placed, graded, passed, failed (by who the failure is charged to), void, ungradeable, ungraded.
- The pass rate of graded calls, of placed calls and of everything attempted, each with its counts and interval, and the rate by scenario.
- The gate for the claim: passed, counted, how many are needed with that many failing, and the true rate the runs show.
- Every placed call left out of "of graded", with its reason, and every call not run.
- Failures grouped by fault class and last step.
- Each said, heard, stored disagreement, with where "stored" was taken from.
- Which clips had a label no person has confirmed, and which were a synthetic voice.
- What the report does not show, in plain words: for example that the calls went over a direct connection and not a phone line.

### The three verdicts

| Verdict | When |
|---|---|
| The claim is met | The runs demonstrate the claimed rate at the stated confidence, by scenario, and nothing refuses the gate |
| Not enough calls to support the claim | What was observed is at or above the claim, and there are too few independent results to demonstrate it |
| The claim is not met | The observed rate is below the claim, or the gate is refused because too much of what was attempted was never judged |

The page never says "passed" of the agent. Twelve scenarios that all pass read "not enough calls to support the claim": 95% at 95% confidence needs 59 independent results with none failing, 93 with one, 124 with two. A scenario counts once and passes only if every call placed for it passed, because repeats replay the same audio.

Exit code 0 means the claim is met, 1 that it is not or that there are too few calls, 2 that no report was made.

### When no report is made

- There are no runs.
- Any run is a rehearsal against a scripted stand-in.
- The same scenario file answers to more than one version across the runs. `--allow-mixed-scenarios` goes on and lists the versions on the page.
- Any run rests on a scenario that states no expectations (the same check as `validate --strict`). Each run records `states_expectations` when it is placed, and a batch that held such a scenario back says so in `batch.json`; either one refuses the report and names the file. There is no switch: state the expectations and run the scenario again.
- Any run has no `states_expectations` field at all. It was stored before the field existed, so nothing says whether it asked anything of its call: the report is refused with "run <directory>/<run> predates expectation recording: re-run it". Chain calls are placed the same way and carry the field too.
- A directory has no manifest. `--allow-no-manifest` goes on and the page says what could not be recorded.

### Numbers

Every percentage has its counts beside it. A percentage is never printed on the other side of the claim from the true value: 94.96% against a claim of 95% is printed as 94.96%, not 95.0%.

The page depends only on the run directories and the options given, apart from the line saying when it was generated. It names directories by their last part only, never by full path.
