# Onboarding a new target

This guide takes you from nothing to one graded run, without us. It has seven
steps, and the last one places no call: it uses the fake leg, a scripted
stand-in for an agent. A run on the fake leg shows that your setup works. It
shows nothing about any agent, and every result it writes says so.

Placeholders in angle brackets (`<conversation-id>`, `<case-name>`) are for you
to fill in. Nothing here is a real name, number, address or key.

## Before you start

You need Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and ffmpeg
(only for cutting clips from a captured call). Then:

```
cd caller
uv sync --all-extras --frozen
export PATH="$PWD/.venv/bin:$PATH"               # puts synthiq-caller on PATH (bash, sh, scripts)
synthiq-caller doctor --fake-only                # what the fake leg needs
```

Use `export PATH=...`, not `alias`: an alias exists only in an interactive
shell, so it fails in a script, in CI and under `sh -c`. The path is absolute,
so it keeps working after `cd`. In a new terminal, run the `export` again from
`caller/`, or call `uv run synthiq-caller` from there each time.

Make a working folder for the target, **outside any repository** or somewhere
git ignores. It will hold recordings of people:

```
synthiq-caller init ~/targets/<target-name>
cd ~/targets/<target-name>
```

`init` writes a profile (`target.json`, which allows no mutation), three
read-only scenarios, a scripted agent for each, three tones standing in for
recordings (`clips/`, marked synthetic), `env.example`, and a `.gitignore` that
ignores `cases/`, `runs/`, `clips/` and audio files. It refuses to overwrite
what is already there unless you pass `--force`.

## Step 1. Connect the platform's key

The key is read from the environment when a command runs. The tool never writes
it to disk and never prints it.

```
export ELEVENLABS_API_KEY=...        # in your shell only; never in a file that is committed
synthiq-caller doctor .              # says whether it is set, never what it is
```

`doctor` reports two legs. **Fake leg** is what a rehearsal needs (no key, no
network): it decides the exit code, so `doctor .` exits 0 without a key. **Live
leg** is what a real call needs (the key, a known region, ffmpeg). Without a key
it prints `live leg: NOT READY (key)`, still exiting 0 unless you add `--live`,
which exits 1 when the live leg is not ready. Even when it says the settings are
present, that is only the settings: nothing was called, and only a call shows
the platform accepts the key.

The fake leg is judged on a folder: every scenario in `scenarios/` is read with the
loader a run uses, so a file that does not load fails it. `doctor` with no folder
checks only the machine and prints `fake leg: not checked`, never `ready`.

`env.example` lists the variable names and holds no values. Use another variable
with `--key-env <NAME>`. The key should be read-only where the platform allows
it: `capture` only reads.

## Step 2. Pick the region

An account on a regional (residency) host must use that host, or every request
is refused. A residency environment is a separate account and needs its own API key:
a key from the global account does not work there. The regions are `global` (the default), `us`, `eu`, `in` and `sg`.

```
synthiq-caller doctor . --region <region>
```

It prints the address that region maps to. `capture`, `run`, `batch` and `regrade` take the same word with
`--region <region>`, or read it from `ELEVENLABS_REGION`; `doctor` reads it from there too; an unknown name, from the flag or the
environment, is an
error (exit 1), never the global host. To check the address answers at all, add `--check-network`;
that is the only thing that touches the network, and it sends no key.

## Step 3. Fetch a sample of recent calls

Take a handful of conversation ids from the platform's own call list (the rig has
no command that lists calls), choose ones that cover different kinds of call, and
capture each:

```
synthiq-caller capture --trace elevenlabs --region <region> \
    --conversation <conversation-id> --out cases/<case-name>/
```

Each capture makes two read-only requests and writes the conversation as
received, the audio, the platform's own transcript, a normalised trace and a
record of what was fetched. Three things to know:

- A capture holds a **recording of a person**. `capture` refuses an `--out`
  inside a git work tree that git does not ignore; `cases/` in the folder from
  `init` is ignored. Do not move captures into a repository, and do not send them
  to any service you have not cleared.
- The transcript is a machine's reading of the call, not the truth of what was
  said. A platform may keep no audio at all; then `capture` says so and exits 1,
  and `ingest` has nothing to cut.
- `capture` supports ElevenLabs only. Other platforms are in step 4.

To turn a capture into clips and a scenario skeleton, run
`synthiq-caller ingest cases/<case-name>/`. That cuts each caller turn from the
platform's whole-second turn times, so every clip is flagged and **a person must
listen to each one** before the clip is trusted (see the README, "Capture a live
call into a test"). The skeleton states no expectations; step 6 covers that.

Every clip cut from a captured call is a **real** recording and is special-category
data. Before a batch, run or `validate` will accept it, fill in two columns of
`clips/manifest.csv` for each clip: `lawful_basis` (the Article 6 basis and the
Article 9 condition you rely on) and `consent_ref` (an id pointing to where the
consent record is held, never the record itself). `ingest` writes both empty on
purpose, so a clip you have not decided on is refused, not played. The tones from
`init` are marked `synthetic` and need neither. Which basis applies is your decision,
not the tool's; see `docs/CLIP-CONSENT-AND-FORGET.md`.

## Step 4. Choose a trace adapter

The trace adapter reads back what the agent did during a call: which tools it
ran, with what, and what they answered. Pass it as `--trace <platform>` to `run`
and `batch`, and to `capture`.

| Platform | `--trace` | State |
| --- | --- | --- |
| ElevenLabs Agents | `elevenlabs` | Run against a real account |
| Retell | `retell` | Written from the published reference; not yet compared with a live account |
| Vapi | `vapi` | Written from the published reference; not yet compared with a live account |

`docs/ADAPTERS.md` says what each platform gives, what it does not, and what the
adapter does about each gap. A gap is marked, never filled with a guess; a check
that needs a missing value is "not checked", and the call is ungradeable, never
passed. Read that page before relying on an adapter that has not met a live
account.

## Step 5. Write a first three scenarios from the reference

Start from the three in `scenarios/`. Each is read-only (`"mutates": "none"`)
and states what must be true of the call. Edit them, or write new ones, using
`docs/SCENARIO-REFERENCE.md`: every key a scenario file accepts, its type, its
default and what it means. That page is generated from the scenario schema in
`src/synthiq_caller/scenario.py`, and a test fails when it is out of date.
Regenerate it with:

```
synthiq-caller reference --out docs/SCENARIO-REFERENCE.md
```

Rules that matter for a first three:

- Keep `mutates` at `none`. A scenario that registers, books or moves changes
  real data, and the default refuses to run it. Leave those until the three
  read-only ones pass against a test agent.
- State at least one expectation: a tool that must be called (`expect.tools`), a
  persona field that must reach a tool (`expect.fields`), or something the agent
  must say (`expect.agent_says`). With none, the scenario is **ungradeable**.
- `when` is a pattern over what the agent says. Check each against the agent's
  real first lines, taken from a captured transcript.
- A clip must exist in the bank, a person must have confirmed what it says, and a real
  clip must carry `lawful_basis` and `consent_ref` (step 3).
  Clips from `ingest` are unconfirmed until someone listens and fills in
  `checked_by_human` in `clips/manifest.csv`, or records a correction in
  `overrides.json`.
- A scenario written from a captured call (`ingest`) states no expectations, so
  `validate` warns and `batch` holds it back. That is on purpose. Add the
  expectations before it counts.

## Step 6. Run `validate`

```
synthiq-caller validate scenarios --clips clips --target target.json
```

It places no call. It checks that the files parse, that the clips they name are
in the bank, and that what they require of the system is stated. Read every line:

- `CLIPS NOT USABLE`: a clip is missing or ruled out.
- `UNGRADEABLE until expectations are stated`: the scenario asks nothing of the
  call. The command still exits 0, because an unfinished file is not a broken
  one. Do not treat that exit code as "ready".
- `TARGET PROFILE LACKS ...`: the scenario leaves a part of its booking to the
  profile and `target.json` does not give it.

**Use `--strict` before anything counts.** `synthiq-caller validate scenarios
--clips clips --strict` turns every UNGRADEABLE line into an error and exits 1,
naming each scenario. Acceptance runs and the CI gate always apply this check,
and acceptance refuses a run whose scenario stated no expectations. A capture
you have not finished is expected to fail it; fill in the expectations, then
run it again.

## Step 7. Run one lookup-only call

The first run uses the fake leg:

```
synthiq-caller batch scenarios --leg fake --fake-agent agent-scripts \
    --clips clips --target target.json --out runs --settle 0
synthiq-caller report runs
```

The fake leg plays your clips into a scripted agent (`agent-scripts/<scenario
file name>`, each a list of lines the agent speaks and the trace it leaves) and
grades the result like any other call. You should see three runs, each `passed`
or `failed`, never `ungraded`. Every run and the report are marked
**REHEARSAL**, no gate can be met by one, and the clips are tones, not speech.
To see that grading is real, change a value in one script so the tool receives
something other than the persona's fact: that run fails.

If you see `ungraded` or `not run`, the report says why next to each.

`batch` exits 0 even when runs failed, unless you pass `--fail-under <percent>`;
read the report, or add `--fail-under`, before treating an exit code as a pass.

### A real call

A real call goes to an agent and may change data. **Do not place one without the
owner's go-ahead**, and only against the agreed test agent, never one that takes
real callers.

- Keep the mutation guard on: `--allow-mutating none` is the default and holds
  back every scenario that is not read-only. Do not widen it for a first call.
  `allow_mutating` in `target.json` is a ceiling a batch cannot exceed.
- Use `--dry-run` first: it prints what would run and what would be held back, and
  places nothing.
- Then one scenario, once:

```
synthiq-caller batch scenarios --dry-run --leg elevenlabs-socket --agent <agent-id> \
    --clips clips --target target.json --out runs
synthiq-caller batch scenarios --leg elevenlabs-socket --agent <agent-id> \
    --clips <real clip bank> --target target.json --out runs --only <scenario> --repeats 1
```

The tones from `init` are not speech; a real call needs a bank of real
recordings (from `ingest`, listened to and confirmed) or an agreed synthetic
voice. A real run is only a measure of the agent if its trace was read: a call
whose trace could not be fetched is `ungraded`, never passed.

## What this does not prove

- The fake leg proves your folder and the rig agree. It says nothing about an
  agent.
- A passing `validate` says files are well formed, not that they are good tests.
- Nobody but you can say the guide works on a machine you did not set up: have a
  second person follow it from a clean checkout and tell us where they stopped.
