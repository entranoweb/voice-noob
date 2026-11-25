# Target profiles, rehearsals, and what changed after the post-merge review

This page covers what was added or tightened after the review of 25 November 2026
(`docs/audits/2026-11-25/post-merge-review.md`) and the live re-run of the same day.
The outcome classes themselves are in `OUTCOMES-AND-GATE.md`.

## A target's profile

A scenario says what the caller wants. When the slot is, which days the clinic works
and what zone its database writes times in belong to the system under test, so they
live in a profile, one JSON file per target:

```json
{
  "name": "original-clinic",
  "timezone": "Europe/London",
  "naive_times": "UTC",
  "identity_budget": 2,
  "fixture_faults": {"sophie_v2_register_patient": ["DUPLICATE_FOUND"]},
  "bookings": {
    "01-consultation-self-pay-shoulder": {"time_from": "16:30", "time_to": "16:30"}
  }
}
```

- `bookings` is keyed by scenario file name without its ending.
- A scenario lists the parts it leaves to the profile in `expect.booking_from_target`.
- Run with `--target <file>` on `batch`, `validate` and `regrade`.
- A scenario whose parts the profile does not give is **held back** ("the target profile
  does not give ..."). It is never run with a weaker check.
- `scenarios/sophie/targets/original-clinic.json` holds the times the recordings were
  made against. `test-clinic.json` is there to be filled in; until it is, the booking
  scenarios do not run against the test clinic.
- `naive_times` is the zone the target writes a start time in when it gives no offset.
  A database that stores UTC without saying so is `"UTC"`. Left out, such a time is
  taken to be in `timezone` already.

## Rehearsing without an agent

```
synthiq-caller batch scenarios/ --leg fake --fake-agent agent.json --clips bank/ --out runs/
```

`--fake-agent` is one script, or a directory with one script per scenario file
(`default.json` for the rest):

```json
{
  "lines": [{"text": "How can I help?"}, {"text": "Your date of birth?", "hears": "20 Feb"}],
  "trace": {"tool_calls": [{"name": "lookup", "arguments": {}, "result": {}, "ok": true}]}
}
```

A rehearsal shows that the rig runs end to end. It shows nothing about any agent.
Every run file it writes carries `"rehearsal": true`, the batch file does too, `report`
opens with a REHEARSAL line, and `gate` refuses a directory that contains one.
Anything that uploads or displays runs must show that mark.

## How a call ended is part of the verdict

- A call the agent hung up on before the scenario's end, or whose line dropped, cannot
  pass, even when every tool check holds. It is `failed`, fault `unknown`.
- A scenario for which the agent ending the call is the proper end says so:
  `"agent_may_end": true`. A dropped line is never a proper end.
- A crash inside the rig (`rig error: ...`) is `void`, fault `rig`, with or without a trace.
- A question the caller had no answer to makes the call `void`, fault `rig`, whenever the
  call then went wrong, however it ended. An agent that gives up on a silent caller says
  goodbye, and that goodbye used to count as the scenario reaching its end.
- A tool reply that carries one of the target's `fixture_faults` makes a call that went
  wrong `void`, fault `fixture`: the system was not in the state the scenario needs.

## Booking values and refusals

- The booked day, time and type are read from the tool that writes the appointment
  (`expect.booking.tool`), not from a later lookup that happens to carry a start time.
  With no tool named, tools that disagree leave the value unknown.
- A read-back that names a value and holds nothing under it (`null` or `""`) **fails** the
  check. Only a read-back that does not mention the value falls back to what the tool was
  sent, labelled "sent, not confirmed stored".
- `{"name": "verify", "ok": false}` means the tool itself refused, as the last word on the
  matter. A request that failed on the way (a 500, a timeout) is unknown, not a refusal. A
  refusal the agent then got past is not a refusal unless the scenario adds
  `"at_least_once": true`.
- A time window may cross midnight (`22:00` to `02:00`).

## The gate

`synthiq-caller gate runs/ --rate 0.95 --max-unjudged 0.1`

- A scenario passes only when every call placed for it passed, void calls aside. A call
  that was placed and could not be graded counts against.
- The gate is refused, whatever the numbers, when more than `--max-unjudged` of what was
  attempted went unjudged (void, ungraded, ungradeable, not run). The default is 10%.
- A rehearsal never meets a gate.

## Commands the rig runs

Hooks, the reset, the read-back and the prerequisite checker run the operator's command
**without a shell**. The command is split into a program and its arguments once; pipes,
`&&` and `%NAME%` or `$NAME` substitution do nothing. For those, name a script.

- A call reference that is not made of letters, digits and `. _ - :` is never given to a
  command, as an argument or in the environment.
- On Windows a batch file (`.cmd`, `.bat`) is refused when a value it would be given holds
  characters `cmd.exe` would run.

## Other changes

- A directory of runs is checked against the plan the batch wrote before its first call
  ([COMPLETENESS.md](COMPLETENESS.md)); a stopped batch exits 130 (SIGINT) or 143 (SIGTERM).
- Run files are numbered from the highest number in use and are never written over.
- A baseline made before these changes is refused (`version 2`); make it again.
  `compare` reports a scenario that lost all its graded calls as a regression.
- Windows needs the `tzdata` package (now a dependency there). Without a zone database
  the error says so.
- Excluded clips are found before a call is placed, in fallbacks and chain calls too.
- A request with no question mark ("Please tell me your date of birth.") is a question.
  In a turn that asks for two things, the caller answers the things asked for, not a fact
  the agent only acknowledged.
