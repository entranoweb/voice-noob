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
| `leg.py`, `turns.py`, `clips.py`, `scenario.py`, `director.py`, `grading.py`, `stats.py`, `mulaw.py` | `legs/` (sending audio), `traces/` (reading tool calls), and the leg-name switch in `cli.py` |

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
- **Legs**: `elevenlabs_socket` (works, verified live), `fake` (tests).
- **Traces**: `elevenlabs` (workflow and plain agents).
- **Statistics** (`stats.py`): pass rate with a Wilson interval.

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

## Tests

```
uv run ruff check src tests && uv run mypy && uv run pytest
```

No test touches the network. The trace reader is tested against two stored
conversations in `tests/fixtures/` (synthetic test callers).

## What is not built

- **The phone leg.** `legs/twilio_media.py` is a stub that raises
  `NotImplementedError`; its docstring holds the plan (Pipecat with a carrier
  media stream, then SIP).
- **Resetting the system under test** between calls (diary, records, locks).
- **Chains of calls** that share state (register, ring back, get locked out).
- **A referee recogniser.** `Heard` is whatever the engine reports. An engine
  that reports nothing leaves that column empty.
- **Agent text on a phone leg.** A carrier gives audio only, so steps keyed on
  the agent's words will need the referee recogniser.
- **Keypad digits on the socket leg.** The websocket has no keypad channel.

## Honest limits

- **The socket is not a phone line.** Audio over the websocket skips the
  carrier and the telephone codec. In five socket runs the recorded clip
  "5th July 1987" was heard correctly five times, although the same words on
  the phone were heard as "6th July" and stored that way. Phone-only mishearing
  needs the phone leg.
- **The same clip is not heard the same way twice.** One opener clip was heard
  three ways in five runs: "Sorry, can you book...", "Droitwich, any book...",
  "Surely there will be...". Run each scenario several times and read the
  interval, not a single result.
- **Clips come from mixed recordings.** They were cut on word timings from
  recordings that hold both voices; clips overlapping the agent's speech were
  dropped, and none has had a human listen yet (`checked_by_human` is empty).
- **Dates are parsed day first** and need a year. Other formats come back as
  "not understood", which reads as a mismatch.
