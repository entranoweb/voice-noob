# Reset, read-back, chains and baselines

Four additions to the batch runner. None of them knows what system the agent is
connected to: the operator supplies a command or an address for each.

Nothing here has been run against a live agent. Everything is tested with the
scripted agent only.

## Reset: a clean start before a call

A scenario that only gives the same answer from a clean start says so:

```json
{ "name": "books the last free slot", "clean_state": true, "mutates": "books", "steps": [] }
```

The batch runs its reset before each such call.

| `--reset` | What it does |
| --- | --- |
| left out | Scenarios that ask for a clean state are **not run** |
| `none` | The operator states the system is already clean; nothing is done |
| `command` | Runs `--reset-command`; exit code 0 within `--reset-timeout` means clean |
| `http` | Calls `--reset-url` with a secret header; any 2xx answer means clean |

- A reset that fails marks the call **not run**. It is never counted as a failed call.
- The command gets `SYNTHIQ_SCENARIO_FILE`, `SYNTHIQ_SCENARIO_NAME`,
  `SYNTHIQ_MUTATES`, `SYNTHIQ_REPEAT`, `SYNTHIQ_CHAIN` and `SYNTHIQ_PHASE=reset`
  in its environment. The address gets the same as a JSON body.
- The secret is read from the variable named by `--reset-secret-env` and sent in
  the `--reset-header` header (default `X-Synthiq-Secret`). It is never printed.
- A plain `http` address is refused unless it is this machine.
- A batch that resets cannot run calls in parallel.

## Read-back: what the system actually holds

A tool that answers "booked" is the agent's account, not proof. A read-back asks
the system itself after each call and answers with one flat JSON object:

```json
{ "date_of_birth": "2002-02-20", "appointment_status": "booked" }
```

| `--read-back` | "stored" comes from |
| --- | --- |
| left out, or `none` | What the agent's tool was sent, labelled `tool call` |
| `command` | What `--read-back-command` prints |
| `http` | What `--read-back-url` answers |

- The four caller facts use their scenario names: `name`, `date_of_birth`,
  `postcode`, `phone`. When the read-back has a value for one, that value is
  "stored" and the row says `read back`. When it has none, or the read-back
  failed, the grader falls back to the tool call and the row says `tool call`.
- Every result shows the source: `stored_source` on each field row, a
  `stored from` column in the per-call report, a `read back` count in the
  summary, and `read_back` / `read_back_error` in each `run-NNN.json`.
- Any other key can be required with `expect.read_back`:

  ```json
  "expect": { "tools": [{ "name_contains": "book" }], "read_back": { "appointment_status": "booked" } }
  ```

  With no read-back those checks cannot be made, and a call that otherwise
  passes is left **ungraded**, not passed.
- The command gets `SYNTHIQ_CALL_REF`, the scenario variables and one
  `SYNTHIQ_FACT_<NAME>` per caller fact. The address gets the same as JSON.

## Chains: calls that share state

A file with a `chain` key holds an ordered list of calls. Each call is a normal
scenario plus an `id` and, optionally, a `capture` block.

```json
{
  "name": "book then cancel",
  "clean_state": true,
  "chain": [
    { "id": "book", "mutates": "books", "steps": [],
      "expect": { "tools": [{ "name_contains": "book" }] },
      "capture": {
        "appointment": { "tool": "book", "result": "appointment.id" },
        "day": { "tool": "book", "argument": "day" } } },
    { "id": "cancel", "mutates": "moves",
      "steps": [{ "when": "which day", "clips": ["days/{{day|slug}}.wav"] }],
      "expect": { "tools": [{ "name_contains": "cancel",
                              "arguments": { "appointmentId": "{{appointment}}" } }] } }
  ]
}
```

Where a captured value can come from:

| Capture | Value |
| --- | --- |
| `{"tool": "book", "argument": "day"}` | An argument of the last successful matching tool call |
| `{"tool": "book", "result": "appointment.id"}` | A dotted path into that call's result |
| `{"field": "date_of_birth", "from": "stored"}` | A caller fact as `said`, `heard` or `stored` |
| `{"agent_says": "reference (\\w+)"}` | Group 1 of a pattern over the agent's lines |
| `{"read_back": "appointment_id"}` | A key of the read-back |

Later calls use a value as `{{name}}` anywhere in their definition. Filters:
`{{name|slug}}` for a file name, `{{name|re}}` inside a pattern, `{{name|lower}}`.

Rules:

- A chain is graded as one unit (`chain-NNN.json`) and call by call (`run-NNN.json`).
- Once a call does not pass, the later calls are **not run**. They are listed as
  not run, never as failed.
- The mutation guard covers the whole chain. If any call is above what the batch
  allows, no call is placed. A chain's level is that of its most dangerous call.
- `clean_state` goes on the chain. The reset runs once, before the first call.
- Every call must expect something, and may only use values an earlier call captures.
- Chain verdicts: `passed`, `failed`, and three that prove nothing: `not run`,
  `ungraded`, `incomplete`.
- `expect.tools[].arguments` is new and also works in single-call scenarios.
- `regrade` regrades chain calls with the values they ran with.

## Baseline and compare

```
synthiq-caller baseline runs/monday --out baseline.json
synthiq-caller compare runs/tuesday --baseline baseline.json --fail-on beyond-noise
```

`compare` lists every scenario and chain whose pass rate dropped, with the 95%
Wilson interval of both rates. A drop is **beyond noise** only when the two
intervals do not overlap. With few repeats most drops are within noise; the cure
is more repeats. `--fail-on any` exits 1 on any drop, `beyond-noise` only on a
clear one, `never` (the default) always exits 0.

## Keypad digits over the socket leg

The ElevenLabs conversation socket carries no tones. Its published message
schema allows a client `user_message` with `"source_medium": "dtmf"`, and
`--keypad-as-message` sends digits that way. It is off by default and **not
checked against a live agent**: the schema lists the value without saying what
the agent does with it. Real tones need the phone leg.
