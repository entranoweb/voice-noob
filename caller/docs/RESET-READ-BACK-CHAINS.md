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

## Wiring a target: reset, adopt and read-back from its profile

A target's profile (`--target`, see `TARGETS-REHEARSAL-AND-SAFETY.md`) can also say
how the rig reaches that system around a call, so a batch needs one flag, not six.
None of this has been run against a live system; it is tested with fakes only.

```json
{
  "name": "my-target",
  "allow_mutating": "books",
  "reset":       { "command": ["npm", "run", "fixture", "--", "restore"], "timeout_s": 300 },
  "before_call": { "command": ["python", "tools/check.py"] },
  "after_call":  { "command": ["npm", "run", "fixture", "--", "adopt", "--conversation", "{call_ref}"] },
  "read_back":   { "command": ["python", "scripts/read_back_sql.py", "--queries", "queries.json"] }
}
```

- A command is a **list**: the program, then each argument. A single string is refused.
  Nothing goes through a shell, and nothing splits a list again, so an argument with
  spaces stays one argument.
- Commands run from the directory the batch is started in.
- `after_call` may name `{call_ref}` (the platform's conversation id), `{run_file}`,
  `{run_id}`, `{scenario_file}` and `{repeat}` inside any argument. A reference that is
  not a plain id is never handed over. This is where a step that records "this call
  created these rows, they are the rig's to remove" belongs. With
  `"only_when_mutating": true` it is skipped for a call whose scenario says
  `"mutates": "none"`; a scenario that does not say what it changes still gets it.
- `allow_mutating` is the most a batch may change on this system. It does **not** switch
  mutation on; `--allow-mutating` on the batch still does that. A batch that asks for
  more than the profile permits stops before anything is loaded or run.
- What the command line gives wins: `--reset`, `--read-back`, `--before-call` and
  `--after-call` each replace the profile's.
- `scenarios/example-receptionist/targets/wired-target.example.json` is a filled-out example with
  placeholders. Keep a real copy outside the repository if it names anything real.

### A chain's appointments in the profile

A call inside a chain takes its bookable times from the profile like a single call
does. Its entry in `bookings` is `<chain file name>/<call id>`:

```json
"bookings": { "book-then-cancel/book": { "time_from": "10:00", "time_to": "10:00" } }
```

A chain with a call whose parts the profile does not give is held back whole, and
`validate --target` lists them (`TARGET PROFILE LACKS ['book: time_from']`).

### See it before it runs

```
synthiq-caller batch scenarios/ --target my-target.json --leg fake --clips bank/ \
    --out runs/ --allow-mutating books --dry-run
```

`--dry-run` prints which files would be held back and why, and for the rest the reset,
hooks and read-back exactly as the lists they would be run as. It runs no command, calls
no address, reads no secret, places no call and writes nothing.

### The read-back script for a PostgreSQL system

`scripts/read_back_sql.py` is a read-back command for a system whose records are in
PostgreSQL. It knows no table: a queries file says what to select
(`scripts/read_back_queries.example.json`).

| Where | What |
| --- | --- |
| `SYNTHIQ_READBACK_DSN` | The connection string, for a read-only role. Never in a file or an argument |
| `SYNTHIQ_READBACK_SCOPE_ID` | The one tenant, clinic or account every query is held to |
| `--queries <file>` | The selects, each with the keys it answers (`columns`) |
| `--dry-run` | Checks the file and prints the queries; connects to nothing |

- Every query must be one `select` that uses `%(scope_id)s`. Anything else is refused
  before a connection is made. The session is read-only as well, so the database
  itself refuses a write.
- A `sanity` query must find a row first. A role that cannot see the scope gets zero
  rows from every table, which would otherwise read as "the agent stored nothing".
- No row: every key of that query is answered `null`, which the grader counts as
  **not stored** (a failed check, never a fall-back to what the tool was sent).
- A query whose `needs` are not known for this call is skipped and answers nothing.
- It needs `psycopg` (`pip install "psycopg[binary]"`), which the caller itself does not.

### What a person fills in for a real target

1. In a copy of the example profile: the product checkout's path, the database host the
   fixture tool asks to have confirmed, the path to this package, and the path to the
   queries file.
2. In the queries file: the real table and column names, the name of the row-level
   setting if the database uses one (`settings`), and a `sanity` select.
3. In the environment of the shell that runs the batch: `SYNTHIQ_READBACK_DSN`,
   `SYNTHIQ_READBACK_SCOPE_ID`, and whatever the fixture tool itself needs.
4. In `bookings`: the days and times that system can offer, per scenario and per chain call.

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
