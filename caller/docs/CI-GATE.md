# A CI gate and scheduled runs

How a batch of caller scenarios is run on a schedule, set beside a stored
baseline, and made to fail a build when it gets worse. Two parts: the workflow
this repository runs for itself (`.github/workflows/caller-scheduled.yml`), and
a pattern you can copy into your own CI.

## What the repository's workflow does, and does not

It runs a named scenario set on the **fake leg**: a scripted stand-in for the
agent. That means:

- it calls no voice platform and needs no secret on the default path;
- it plays generated tones, never a recording of any person;
- every run it writes carries `"rehearsal": true`, and the log, the job
  summary, the scorecard and the upload all say REHEARSAL. A rehearsal shows
  that the rig, the scenarios and the baseline still hold together. It shows
  nothing about any agent, and no acceptance gate can be met by one.

A run against a real agent uses the same steps with another leg. It needs a
clip bank held somewhere private, which this workflow does not have yet.

### Settings it reads

| Setting | Kind | Meaning |
| --- | --- | --- |
| `CALLER_ALLOWED_TARGETS` | repository variable | Comma- or line-separated target names the workflow may run against. Empty or missing means nothing runs. Exact match, no wildcard. |
| `CALLER_SCHEDULED_TARGET` | repository variable | The target a scheduled run names. Must itself be on the list. |
| `SYNTHIQ_UPLOAD_URL`, `SYNTHIQ_API_TOKEN`, `SYNTHIQ_UPLOAD_WORKSPACE` | secrets (the workspace id may be a variable) | All three must be set for the dashboard upload. If one is missing the step prints `upload skipped: not configured` and uploads nothing. |

A manual run (`workflow_dispatch`) takes `scenario_set` (a folder under
`caller/ci/scenarios`, default `smoke`) and `target` (default: the scheduled
target). Until `CALLER_ALLOWED_TARGETS` is set the workflow fails at its first
step; that is intended.

Anyone who can change the workflow file on the default branch can change what
it checks. Protect `.github/` with branch protection or CODEOWNERS.

### Order of steps

1. **Check** (`python -m synthiq_caller.ci_gate check`): refuses a target not on
   the list, an empty list, a set name that is not a plain folder name, a set
   that cannot be loaded, and any scenario or chain whose `mutates` is more
   than the batch's own `--allow-mutating none` allows. A scenario that does
   not say what it changes counts as changing something. It also refuses a set
   holding a scenario that states no expectations (it could never pass): the
   same check as `synthiq-caller validate --strict`, one function used by both.
   The workflow then runs `validate --strict` as a step of its own, so the log
   shows it, still before the bank and the batch, so before any call.
2. **Bank** (`ci_gate bank`): writes generated tones for the clips the set names.
3. **Batch** on `--leg fake` with `--allow-mutating none`. The caller's own
   mutation guard holds a second time, so a mutating scenario is "not run".
4. **Compare** with `baselines/<set>.json`, failing on any regression
   (`--fail-on any`). The fake leg is deterministic, so any drop is a real
   change and not noise. For a live agent, whose results vary, use
   `--fail-on beyond-noise` and enough repeats for it to mean something.
5. **Scorecard** and **summary**, attached to the job and as an artifact, even
   when the compare failed.
6. **Upload** to the dashboard, only when configured.

### The baseline

`caller/baselines/<set>.json` is committed. It holds the pass counts of one
good run per scenario. It is version 2: a baseline made under an older grading
rule is refused, and must be made again. To make or replace it, from `caller/`:

    uv run python -m synthiq_caller.ci_gate bank --root ci/scenarios --set smoke --out /tmp/clips
    uv run synthiq-caller batch ci/scenarios/smoke --leg fake --fake-agent ci/agents/smoke \
      --clips /tmp/clips --allow-mutating none --out runs
    uv run synthiq-caller baseline runs --out baselines/smoke.json

Review the diff of the baseline like code: lowering it is how a regression is
accepted. A new set needs `ci/scenarios/<set>/`, `ci/agents/<set>/` and
`baselines/<set>.json`.

## In your own CI

Neutral on purpose: replace the placeholders with your own. Keep the allow-list
in your CI's settings, not in the repository the pipeline file lives in, and
keep a baseline made from a run you trust.

What to keep from the pattern: refuse targets that are not listed; refuse
scenarios that could change anything on a live system unless you have decided
they may; compare with a stored baseline; keep results marked as rehearsals
when they come from a scripted stand-in; say plainly when a step did not run.

### GitHub Actions

```yaml
name: voice-agent-gate
on:
  schedule:
    - cron: "<minute> <hour> * * *"
  workflow_dispatch:
permissions:
  contents: read
jobs:
  gate:
    runs-on: <your runner>
    steps:
      - uses: actions/checkout@<version>
      - uses: astral-sh/setup-uv@<version>
      - name: Install
        working-directory: <caller directory>
        run: uv sync --all-extras --frozen
      - name: Refuse unlisted targets and mutating scenarios
        working-directory: <caller directory>
        env:
          TARGET: ${{ vars.<TARGET_VARIABLE> }}
          ALLOWED: ${{ vars.<ALLOW_LIST_VARIABLE> }}
        run: uv run python -m synthiq_caller.ci_gate check --target "$TARGET" --allowed "$ALLOWED" --root <scenario sets> --set <set>
      - name: Run and compare
        working-directory: <caller directory>
        run: |
          uv run synthiq-caller batch <scenario set> --leg <leg> --clips <clip bank> \
            --allow-mutating none --out runs
          uv run synthiq-caller compare runs --baseline <baseline file> --fail-on beyond-noise
```

### Any other CI (shell)

```sh
#!/bin/sh
set -eu
# TARGET and ALLOWED come from your CI's settings, never from the repository.
cd "<caller directory>"
uv sync --all-extras --frozen
uv run python -m synthiq_caller.ci_gate check --target "$TARGET" --allowed "$ALLOWED" \
  --root "<scenario sets>" --set "<set>"
uv run synthiq-caller batch "<scenario set>" --leg "<leg>" --clips "<clip bank>" \
  --allow-mutating none --out runs
# Exit status 1 on a regression, on a batch that is not what was planned, and when
# nothing of the baseline ran: the build fails. 2: a file could not be read.
uv run synthiq-caller compare runs --baseline "<baseline file>" --fail-on beyond-noise
uv run synthiq-caller scorecard runs --out scorecard.html
```

`--fail-on beyond-noise` fails only when the new rate's interval no longer
overlaps the baseline's. With few repeats it passes most drops as "within
noise"; the cure is more repeats, not a looser flag. `--fail-on any` fails on
every drop. To fail on an absolute rate as well, add `--fail-under <percent>` to
the batch (a rehearsal can never meet it) or run `synthiq-caller gate runs`.

## Not covered

- Schedule triggers cannot be tried before the workflow is on the default branch.
- The rig cannot see what a live agent did on its own side. A scenario marked
  read-only can still change something if the agent errs, so point a live run
  only at a test agent.
