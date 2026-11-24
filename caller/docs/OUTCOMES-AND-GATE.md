# Outcomes, faults and the gate

What a result from the caller means, and what it takes to claim a pass rate. Written after the validity audit of 25 November 2026.

## Outcomes

Every call that was placed ends as one of five outcomes. None is dropped.

| Outcome | Meaning | Fault | In "of graded" | In "of attempted" |
|---|---|---|---|---|
| `passed` | Every check held, the call reached its end, nothing was unknown | none | yes | yes |
| `failed` | A check did not hold | `agent` | yes | yes |
| `failed` | The call was not answered, died before its end leaving no trace, or ran to the time limit | `unknown` | yes | yes |
| `void` | The test rig spoiled the call: it had no answer to a question, or it broke | `rig` | no | yes |
| `ungradeable` | The call cannot be judged as the scenario stands (see below) | none | no | yes |
| `ungraded` | No trace could be read, or the scenario asks nothing | none | no | yes |

A call the rig decided not to place (mutation guard, name budget, unchecked clips, failed reset) is "not run". It is listed separately and is in neither count.

The report prints the pass rate twice, "of graded" and "of attempted", and lists every call left out of "of graded" with its reason. A `void` call should be re-run. A void rate above a few percent is a defect in the scenarios, not a property of the agent.

### When a call is `ungradeable`

- The scenario's `mutates` is `books` or `moves` and it states nothing about what the appointment should be (`expect.booking`, a tool expectation with `arguments`, or `expect.read_back`).
- A stated part of the booking cannot be found in the read-back or in any tool call.
- A tool expectation depends on a result the platform withheld, or on a field the tool's reply does not carry.

### When a call is `void`

The agent asked a question (a line with a question mark) that no step matched and the scenario has no `fallback`, and the call then did not reach the scenario's end. The unmatched line is stored in the run file under `record.unanswered`; widen the step's `when` pattern with it. A line the scenario forbids (`agent_never_says`) is still charged to the agent.

A transcriber that drops question marks hides this case. On the phone leg, check the transcriber punctuates.

## Booking expectations

A booking tool that answers "success" says only that something was booked.

```json
"expect": {
  "tools": [{"name": "sophie_v2_book"}],
  "booking": {
    "weekdays": ["wednesday"],
    "time_from": "10:00", "time_to": "10:00",
    "type": "mri",
    "clinician": "",
    "start_keys": ["bookedDisplayTime"]
  }
}
```

Each stated part is compared with the appointment as the system holds it: the read-back first (`readback.py`), then the last successful tool call that carried the value, in its arguments or its reply. Lists inside a reply are not searched, because a list is a set of candidates, not the choice. The check says where the value came from.

A start time is read from an ISO date-time (converted to `timezone`, default `Europe/London`) or from words: a day name and a clock time such as "at half past four in the afternoon". An hour with no morning or afternoon is not guessed.

Other parts: `date_from` / `date_to` (ISO dates), `status` (for a cancellation), and `type_keys`, `clinician_keys`, `status_keys` when the defaults do not fit.

## Tool outcomes

A tool can answer at the transport level and still have refused: an identity check that ran and did not match is a successful request.

```json
"tool_outcomes": {
  "sophie_v2_verify_identity": {"path": "verified", "success": ["true"]},
  "sophie_v2_book": {"path": "bookOutcome", "success": ["BOOKED"]},
  "sophie_v2_register_patient": {"path": "newPatientRegistered", "success": ["true"]}
}
```

With a rule, a tool "succeeds" only when it reported one of the `success` values at `path` (`*` accepts any value). The value is looked for in what the engine recorded the tool as reporting (`ToolCall.outputs`, see `TRACE-CHECKS-SCORECARD.md`) and then in the tool's reply. When neither carries the path the check is unknown and the call `ungradeable`. A tool with no rule is read by the platform's own success flag.

A tool expectation's `outputs` (`{"status": "not_found"}`) is checked the same way: when the tool ran and the trace does not carry that name at all, the check is unknown, not failed.

## Prerequisites

Given a checker (`--check-requires-command` or `--check-requires-url`, as for `validate`), a batch asks it about each scenario or chain that has a `requires`. An answer of unmet holds the scenario back as "not run: prerequisite unmet". An error, or no checker, lets it run: the report cannot then tell a fixture gap from an agent fault.

A tool expectation can name a tool exactly with `name`, or by fragment with `name_contains`.

## Said, heard, stored

- "Stored correctly" is counted only when the value was read back from the system under test.
- A right value seen only in a tool's arguments is "sent correctly, not confirmed stored".
- A row is marked "labels unverified" when the clip's label has not been confirmed by a person, and "synthetic voice" when the clip is a synthetic stand-in.

## Clip labels and provenance

A clip's label starts as a recogniser's transcript. A scenario fact written from a wrong label makes a correct agent fail and an agent that makes the same mistake pass.

- A person confirms a clip by filling `checked_by_human` in the bank's `manifest.csv`.
- A batch refuses a scenario that plays an unconfirmed clip. `--allow-unverified-clips` runs it and marks every affected field.
- `--clips-extra <dir>` adds a further bank. A manifest's `provenance` column says `real` (the default) or starts with `synthetic`. A synthetic clip's label is known, since it was made from its text. Each run file lists the synthetic clips it played, and the report counts the calls.

## One turn, two questions

When the step that matches an agent turn carries a persona fact, every other unused step that carries a different fact and also matches the turn is answered too, in the order the turn names them. Steps that carry no fact are never added, so a read-back that mentions the date of birth gets one "yes".

## Identity budget

`identity_budget` on a scenario is how many identity checks its target allows per caller in one window; `identity_attempts` is how many one call spends. The batch refuses a call that would go over, or waits with `--space-names`. `--name-budget-file` keeps the spending in a file so batches run one after another share it. The default budget is 2.

## The gate

`synthiq-caller gate runs/ --rate 0.95 --confidence 0.95` says whether a batch demonstrates a pass rate.

| Claim: true rate of at least 95%, at 95% confidence | Calls needed |
|---|---|
| No failures | 59 |
| One failure | 93 |
| Two failures | 124 |

58 of 60 shows a true rate of about 90%, not 95%.

Two readings are printed. By call, every placed call that is not void counts, and anything not passed counts against. By scenario, a scenario counts once and passes only if every one of its calls passed. Repeats of one scenario replay the same audio and are not independent calls, so the by-scenario reading is the one to quote. The command exits 0 only when the by-scenario reading is met.

## Not verified

Everything on this page is tested against the scripted agent only. No live call and no phone line.
