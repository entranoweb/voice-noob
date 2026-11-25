# Synthiq Voice caller

An automated test caller. It plays a real person's recorded answers into a voice
agent, reacts to what the agent says, and grades the call from the agent's own
tool results.

It exists because the failures that matter on a real line are invisible to text
simulations: a date of birth heard wrongly and stored wrongly, a postcode heard
five ways in five calls, a caller cut off mid-answer. A recorded voice
reproduces them; a typed sentence cannot.

## The one rule: engine independence

The core knows nothing about any voice engine. It talks to a `Leg` (how audio
reaches the agent) and reads a `Trace` (what the agent's tools did).

| Engine-neutral core | Engine-specific, and only here |
| --- | --- |
| `leg.py`, `turns.py`, `clips.py`, `scenario.py`, `director.py`, `grading.py`, `stats.py`, `mulaw.py`, `batch.py`, `report.py`, `clipbank/` | `legs/` (sending audio), `traces/` (reading tool calls), and the leg-name switch in `cli.py` |

A new engine is one leg and one trace reader. Nothing else changes.

## What is built

- **Clip bank** (`clips.py`): a folder of WAV clips with a `manifest.csv`. Clips
  are resampled only when a leg wants another rate (that needs `ffmpeg`).
- **Turn detection** (`turns.py`): decides when the agent has finished speaking
  from the energy of its audio, with a hangover and a minimum speech length.
  Agents keep streaming frames while silent, so "frames are arriving" proves
  nothing.
- **Scenarios** (`scenario.py`): persona facts, steps (`when` the agent says X,
  play these clips or send these keypad digits), behaviours (`delay_s`,
  `talk_over`, `stay_silent`), a hang-up line, and expectations.
- **Director** (`director.py`): runs one scenario on one leg and records a
  timeline. It does not judge.
- **Grading** (`grading.py`): pass or fail from tool results only, plus the
  **said / heard / stored** table for each fact the caller gave.
- **Legs**: `elevenlabs_socket` (works, verified live), `twilio_media` (the
  phone leg: built and tested against a stand-in for Twilio, no real call
  placed yet; see [docs/PHONE-LEG.md](docs/PHONE-LEG.md)), `fake` and
  `fake_twilio` (tests).
- **Traces**: `elevenlabs` (workflow and plain agents).
- **Statistics** (`stats.py`): pass rate with a Wilson interval.
- **Batch runner** (`batch.py`): a directory of scenarios, several times each,
  behind a mutation guard, a per-name budget and reset hooks.
- **Report** (`report.py`): pass rate overall and per scenario, the said, heard,
  stored tally per field, where each failure stopped, and what was not run.
- **Clip bank builder** (`clipbank/`): cuts caller-only clips from recordings
  and their word-level transcripts, labels them, and turns one recorded call
  into a scenario skeleton.
- **Scenario set** (`scenarios/sophie/`): a 20-call script for one agent.

## Run it

```
cd caller
uv sync --all-extras
export ELEVENLABS_API_KEY=...        # or pass --env-file <path>
uv run synthiq-caller run scenarios/new-patient-date-of-birth.json \
    --leg elevenlabs-socket --agent <agent id> \
    --clips <clip bank dir> --repeats 3 --out runs/
uv run synthiq-caller report runs/
```

Output of one live run against a test agent (24 Nov 2026):

```
run 1: passed  (53.31s, caller hung up: scenario end)
  [pass] tool 'new_patient_check' is called and succeeds
  [pass] tool 'register_patient' is not called
  [pass] tool 'book' is not called
  [pass] 'name' reaches a tool as the caller said it
  [pass] 'date_of_birth' reaches a tool as the caller said it
  field          said                   heard                      stored                   verdict
  name           Alice Wonderland       It's Alice Wonderland.     Alice Wonderland         stored correctly
  date_of_birth  2002-02-20             It's 20th of February, 20  20th of February, 2002   stored correctly

passed: 1 of 1 (100%; 95% interval 21% to 100%)
```

The shipped scenario hangs up before anything is registered or booked. A
scenario that lets the agent register, book, move or cancel changes real data in
whatever system the agent is connected to. Point those at a test system only.

## Call over a phone line

```
uv run synthiq-caller-phone --dry-run \
    --public-url https://caller.example.com \
    --from +44XXXXXXXXXX --to +44XXXXXXXXXX \
    --scenario scenarios/new-patient-date-of-birth.json \
    --clips <clip bank dir> --out runs/ --port 8765
```

`synthiq-caller-phone` dials through Twilio and plays the clips over the call,
so the audio passes the telephone codec the socket leg skips. `--dry-run`
places no call and needs no credentials. Drop it for a real call, which needs
a Twilio account and number, an address Twilio can reach, and a transcriber
for the agent's side (a carrier gives audio, not text).
[docs/PHONE-LEG.md](docs/PHONE-LEG.md) has the requirements, the checks to make
in order on the first call, and what is still unverified.

## Run a batch

Four commands cover a set of scenarios: `validate` checks every scenario parses
and names clips that exist, `batch` places the calls, `report` reads the
stored runs, and `regrade` grades stored runs again after a scenario is
corrected, without calling.

```
uv run synthiq-caller validate scenarios/sophie --clips <clip bank dir>
uv run synthiq-caller batch scenarios/sophie --leg elevenlabs-socket --agent <agent id>     --clips <clip bank dir> --repeats 3 --out runs/
uv run synthiq-caller report runs/ --summary-only
uv run synthiq-caller regrade runs/ --scenarios scenarios/sophie      # after fixing a scenario; no calls
```

Three guards run before every call.

- **Mutation guard.** Each scenario states, in its `mutates` field, what it can
  change on the system the
  agent is connected to: `none` (lookups only, hangs up before anything is
  written), `registers`, `books` or `moves`. A scenario that does not say is
  `unknown` and is treated as the most dangerous. `batch` runs only `none`
  unless told otherwise with `--allow-mutating registers|books|all`.
- **Name budget.** Systems that look people up by name limit how often one name
  may be checked. The default is 6 calls per name in 15 minutes
  (`--name-budget`, `--name-window`; 0 switches it off). A call over the budget
  is not placed and is listed in the report.
- **Hooks.** `--before-call` and `--after-call` run a command around each call,
  with the scenario in its environment (`SYNTHIQ_SCENARIO_FILE`,
  `SYNTHIQ_SCENARIO_NAME`, `SYNTHIQ_MUTATES`, `SYNTHIQ_REPEAT`,
  `SYNTHIQ_PHASE`). A failing before-hook stops that call. This is where a
  deployment resets the system under test. **No reset is built here**: for the
  ClinWell test clinic the reset lives in that product's fixture scripts and is
  wired in by this option, not by code in this package.

Report of one live batch against a test agent (24 Nov 2026): three lookup-only
scenarios, twice each, six calls.

```
calls: 6
passed: 4 of 6 (67%; 95% interval 30% to 90%)

per scenario
  2 of 2 (100%; 95% interval 34% to 100%)              Caller reports chest pain
  2 of 2 (100%; 95% interval 34% to 100%)              New patient declines the fee
  0 of 2 (0%; 95% interval 0% to 66%)                  Symptom-first opener from a new caller

said, heard, stored (per field, all calls)
  field          answered  stored ok  misheard  mis-stored  not stored
  date_of_birth         2          2         0           0           0
  name                  6          6         0           0           0
  phone                 2          0         0           0           2
  postcode              2          0         2           0           2

failures
  run-001.json  Symptom-first opener from a new caller
    stopped at: step SAY_VERIFY_FAIL: 'I'm sorry, I can't seem to match those up. Let me put you through to reception and they'll'
    failed check: the agent says /been to (see us|the clinic) before/
    failed check: the agent never says /different name/
  run-004.json  Symptom-first opener from a new caller
    stopped at: step SAY_VERIFY_FAIL: 'I'm sorry, I can't seem to match those up. Let me put you through to reception and they'll'
    failed check: the agent says /been to (see us|the clinic) before/
    failed check: the agent never says /different name/
```

What that batch showed:

- **A real fault, found with nobody on the line.** A new caller who opens with
  a symptom is asked "might it be registered under a different name?" and then
  sent to reception, twice out of twice. It is the same fault a person hit by
  phone the same morning.
- **A mishearing.** The recorded postcode "E-zero-one-Z-A" was heard as
  "E01 0A." both times.
- **Two faults in the test itself, both now handled.** One persona's date of
  birth was wrong in the scenario because the clip's transcript was wrong
  ("Sixth in June" for 16 June); the stored runs were graded again after the
  fix with `regrade`, without calling. And one call's trace came back empty
  because it was read too soon; a batch now reads again, and a trace that
  stays empty leaves the call ungraded instead of failed.
- `phone` and `postcode` show "not stored" because the fee was declined before
  the step that would have sent them to a tool.

## The scenario set

`scenarios/sophie/` holds 20 scenarios built from recorded calls to one agent.

| Files | What | Changes on the target |
| --- | --- | --- |
| 01 to 04 | New-patient consultation: self-pay, insured, a single slot answered "Yes. That's fine.", pick a time | `books` |
| 05 to 07 | MRI: referral from a doctor, "how do I submit it", any Wednesday | `books` |
| 08, 09 | Existing patient moves an appointment | `moves` |
| 10, 13 | Postcode said twice; wrong date of birth then the right one | `registers` (a failed check spends an identity attempt) |
| 11, 12 | New caller whose name is close to an existing patient's | `none` |
| 14, 15 | Caller opens with a symptom | `none` |
| 16 | Chest pain | `none` |
| 17 | Fee declined | `none` |
| 18 | Date of birth corrected at read-back | `registers` |
| 19, 20 | Out of scope then "actually shoulder"; booking for a son | `none`, and **not runnable**: each needs a recording that does not exist (`missing_clips`) |

Scenarios 08 to 13 and 15 also carry a `requires` line: what must already exist
on the target (a patient, an appointment). Nothing sets that up yet.

Only the `none` scenarios without a `requires` line have been run live (14, 16,
17). The other seventeen parse and name clips that exist; whether their steps
carry a whole call through has not been tried.

Expectations are graded from tool results first. Where no tool records the
outcome (the agent said to call 999; the agent asked "have you been before?"),
a scenario can require or forbid a pattern in the agent's own words
(`agent_says`, `agent_never_says`). No model's reading of the transcript is
involved.

## Build a clip bank

```
uv run synthiq-caller clipbank build --recordings <dir> --out <dir outside any repository>
uv run synthiq-caller clipbank scenario --clips <bank dir> --call <call id>
```

`build` expects `<call>.mp3` (or `.wav`) with `<call>.stt.json` beside it: a
word-level transcript with speaker ids. For every caller answer it cuts one clip
and writes one manifest row.

- An answer said in two breaths ("B nine ... zero RS") is one clip with its
  pause kept, when no agent word falls between the halves and the gap is under
  2.5 seconds. It is flagged `split_merged`.
- The label comes from the agent line it answers. A short yes or no after a
  read-back is a confirmation (`confirm_fee`, `confirm_details`,
  `confirm_practitioner`, `confirm_booking`, `confirm_choice`), not an answer
  to whatever the read-back mentions.
- `value` is the answer in the form a tool should receive: `1987-07-05`,
  `A56TB`, phone digits, a lower-cased name.
- `review` flags what a person should check: `overlaps_agent` (the agent's
  voice is in the clip), `too_short`, `low_energy`, `split_merged`.

`scenario` lays one call's clips out as steps keyed on the question each
answered. A person then sets the facts, the expectations and `mutates`. This is
the first half of turning a failed call into a permanent test.

**Recorded voices never go in this repository.** `.gitignore` excludes audio and
transcripts under `caller/`; point `--out` somewhere else.

## Capture a live call into a test

```
export ELEVENLABS_API_KEY=...            # environment only; never printed or written
synthiq-caller capture --trace elevenlabs --conversation <id> --out ~/cases/<name>/
synthiq-caller ingest ~/cases/<name>/
synthiq-caller validate ~/cases/<name>/scenarios --clips ~/cases/<name>/clips
```

`capture` makes two read-only requests and writes `conversation.json` (as
received), `audio.<ext>` (the type is taken from the bytes, not the headers),
`transcript.txt`, `trace.json` and `capture.json` (what was fetched, when, with
hashes). It refuses a directory that already holds files unless `--force`, and
says so loudly when the recording could not be had; `ingest` then has nothing to
cut. Use `--api` for an account on a regional host.

`ingest` cuts each caller turn into `clips/`, writes `clips/manifest.csv` and
`scenarios/scenario.json`. No speech-to-text service is called. By default the
cuts come from the platform's turn times, which are whole seconds: each cut is
padded (`--pad`, default 0.5 s a side) and flagged `coarse_cut mixed_audio`,
because the recording is one mixed track and a clip can carry the agent's voice
or lose the caller's first syllable. Listen to every clip. With `--stt <file>`
(word times and speakers from a service you configured yourself, the file
`clipbank build` reads) the cuts are word-level.

What the result claims, and does not:

* Every clip is `label_status=unverified`, `provenance=real-call`, and
  `checked_by_human` is empty: what a clip says is a machine's reading. Consent,
  lawful basis, speaker reference and retention are not filled in (package P8).
* The scenario's steps are the agent's questions in order, as patterns taken from
  the platform's transcript of that one call. Check each pattern against what the
  agent says on a new call. Persona facts are empty, `mutates` is `unknown`, and
  **no expectations are stated**.
* So `validate` prints `UNGRADEABLE until expectations are stated` (exit code 0:
  the file is unfinished, not broken), and `run`/`batch` hold the scenario back
  (`--allow-ungradeable` places the call anyway; it can only ever be ungraded,
  never passed). Add `expect` entries, set `mutates`, and mark clips checked
  (or use `overrides.json`) before using it as a test.

The case directory holds a recording of a person and their machine-transcribed
words. `capture` refuses an `--out` inside a git work tree unless git ignores
it (`git check-ignore`); `caller/cases/` is ignored, so `--out cases/<name>/`
works and anything else in the repository does not.

## Tests

```
uv run ruff check src tests && uv run mypy && uv run pytest
```

No test touches the network. The trace reader is tested against two stored
conversations in `tests/fixtures/` (synthetic test callers).

## What is not built

- **A real phone call.** The phone leg is built, but no call has been placed
  with it; everything about a real carrier is unverified until the first one
  ([docs/PHONE-LEG.md](docs/PHONE-LEG.md), "Not verified").
- **Resetting the system under test** between calls (diary, records, locks).
  The hook points exist; no reset does.
- **Setting up what a scenario requires** (a patient on file, an appointment to
  move). `requires` is a note for people, not something the runner acts on.
- **Chains of calls** that share state (register, ring back, get locked out).
- **A referee recogniser for what the agent heard.** `Heard` is whatever the
  engine reports. An engine that reports nothing leaves that column empty, and
  the phone leg never emits it: read it afterwards from the engine's trace.
  (The agent's own words on a phone call do have a transcriber; see the phone
  leg guide.)
- **Keypad digits on the socket leg.** The websocket has no keypad channel. The
  phone leg plays the keypad tones as audio in the call.

## Honest limits

- **The socket is not a phone line.** Audio over the websocket skips the
  carrier and the telephone codec. In five socket runs the recorded clip
  "5th July 1987" was heard correctly five times, although the same words on
  the phone were heard as "6th July" and stored that way. Phone-only mishearing
  needs the phone leg, which has not yet placed a real call.
- **A clip's transcript can be wrong.** Persona facts taken from the manifest's
  `value` column are only as good as the transcript behind them, and no clip
  has had a human listen. One of three scenarios run live had a wrong date of
  birth for that reason.
- **Seventeen of the twenty scenarios have never been run.** Anything that
  registers, books or moves needs a target that can be reset.
- **The same clip is not heard the same way twice.** One opener clip was heard
  three ways in five runs: "Sorry, can you book...", "Droitwich, any book...",
  "Surely there will be...". Run each scenario several times and read the
  interval, not a single result.
- **Clips come from mixed recordings.** They were cut on word timings from
  recordings that hold both voices; clips overlapping the agent's speech were
  dropped, and none has had a human listen yet (`checked_by_human` is empty).
- **Dates are parsed day first** and need a year. Other formats come back as
  "not understood", which reads as a mismatch.

## What a result means

[docs/OUTCOMES-AND-GATE.md](docs/OUTCOMES-AND-GATE.md) is the reference for the
five outcomes and who each is charged to, booking expectations, reading a tool's
outcome from its reply, unverified and synthetic clips, the identity budget, and
the `gate` command (how many calls a claimed pass rate needs).

[docs/MANIFEST-AND-ACCEPTANCE.md](docs/MANIFEST-AND-ACCEPTANCE.md) covers the
run manifest every batch writes (rig commit, scenario and clip hashes, target)
and the `acceptance` command, which turns run directories into one dated page:
what was tested, what passed, and what that does not show.

See also `docs/TARGETS-REHEARSAL-AND-SAFETY.md`: target profiles (`--target`), rehearsing a batch without an agent (`--leg fake`), and the rules tightened after the post-merge review.
