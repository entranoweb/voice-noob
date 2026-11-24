# What a tool reported, prerequisites, and the scorecard

Four things added after the first lookup-only batch against a live agent showed
their absence.

## What a tool reported: `outputs`

On an ElevenLabs workflow agent, a webhook tool's stored result can be no more
than `{"success": true}` even when the tool answered in full. What the agent
acts on is assigned to variables, and the platform records those assignments
beside the result as `dynamic_variable_updates` (`variable_name`, `new_value`).

The trace adapter now reads them into `ToolCall.outputs`, and reads the tool's
method and address into `ToolCall.request` (headers are never copied). A lookup
that came back "not found" and one that came back "unique" used to look the
same; now a scenario can tell them apart:

```json
"expect": {
  "tools": [
    {"name_contains": "identify", "outputs": {"status": "not_found"}}
  ]
}
```

Each named value becomes its own check ("tool 'identify' reports
status=not_found"), compared as trimmed text without regard to case.

The reader also no longer returns a trace for a conversation the platform has
not finished writing. It waits for status `done` or `failed`; if the wait runs
out it raises `TraceNotFinal`, and the batch records the call as ungraded.

Runs stored before this change have no `outputs`. Fetch them again, placing no
calls:

```
synthiq-caller regrade runs/ --scenarios scenarios/sophie --reread-all --agent - --env-file <file>
```

A trace already stored is never replaced by a failed fetch.

## Prerequisites: "unchecked" is not "ok"

A scenario's or chain's `requires` is free text for people. `validate` used to
print `ok` beside it, which read as "the prerequisite holds". It now reports
three things separately:

```
08-rearrange-....json  mutates=moves  steps=8  clips ok  requires UNCHECKED (Adam Armstrong is registered ...)
```

`requires` is one of `none`, `UNCHECKED`, `met`, `UNMET`, `ERROR`. `validate`
exits 1 only for missing clips, `UNMET` or `ERROR`.

To have it checked, supply a checker of your own. Nothing in this package knows
how to look inside any system.

| Option | Contract |
|---|---|
| `--check-requires-command "<command>"` | Run once per file that has a `requires`. Exit 0 means met, 1 means unmet, anything else is an error. Gets `SYNTHIQ_SCENARIO_FILE`, `SYNTHIQ_SCENARIO_NAME` and `SYNTHIQ_REQUIRES` in its environment. Its last line of output is shown as the detail. |
| `--check-requires-url <address>` | POSTed `{"scenario_file", "scenario", "requires"}`; answers `{"met": true or false, "detail": "..."}`. `--check-requires-secret-env NAME` names the variable holding a secret sent as `X-Synthiq-Secret` (`--check-requires-header` to change it). The address must be https unless it is this machine. |

A batch does not yet refuse to run a scenario whose prerequisite is unmet;
`validate` is the place to find out.

## Run files a person can read

Run, chain, batch and baseline files are written as UTF-8 with the characters
the agent used. Before, `I'm sorry` with a typographic apostrophe was saved as
`I’m sorry`: valid JSON, unreadable in the file. Printed reports are UTF-8
too, including when redirected to a file on Windows.

## Scorecard

```
synthiq-caller scorecard runs/monday runs/tuesday --out card.html \
    --title "Tuesday" --previous last-week.html --faults faults.json
```

One HTML file with no outside assets, in light and dark, holding:

- the pass rate with its 95% Wilson interval;
- a row per scenario, and per chain;
- failures grouped by fault class and last step;
- calls that could not be graded, and why;
- said, heard, stored per field, with how many "stored" values were read back
  from the system under test, and each disagreement listed;
- what was not run, and why.

The numbers are `report.summarise` over each directory, added together.
`card.json` is written beside the page.

**Comparison.** `--previous` takes an earlier scorecard page, its `.json`, or a
file made by `synthiq-caller baseline`. The comparison is `compare.compare`: a
drop is "regressed" only when the two intervals do not overlap, and "regressed
(within noise)" otherwise. The headline before-and-after covers only scenarios
run both times.

**Fault classes.** Whose fault a failure was is a person's judgement. Give it
in a `--faults` file:

```json
{"tuesday/run-005.json": "fixture", "tuesday/run-009.json": "agent"}
```

Classes: `agent`, `scenario`, `fixture`, `rig`, `unclassified`. A failure
nobody has classified is shown as `unclassified`, never guessed. The one
automatic class is `rig`, for a call whose trace could not be read.
