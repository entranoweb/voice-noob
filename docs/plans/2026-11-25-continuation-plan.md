# Synthiq Voice: continuation plan (25 November 2026)

**Status: active.** Written so that any developer, on any model, can pick up one work package and finish it without the session that wrote it. Every package is self-contained: goal, branch, files, done-when, and what not to touch.

Repository: `entranoweb/voice-noob` (product name: Synthiq Voice). `main` is green on `6880803`.

## 1. Where things stand

- **Merged:** pull requests #14 to #27.
- **Works end to end:** the test caller over a direct audio connection to an ElevenLabs agent. It plays recorded answers, reacts to what the agent says, and grades from the agent's tool results.
- **Built, never run live:** the phone leg, reset, read-back, call chains, baseline comparison, Retell and Vapi adapters.
- **Not built:** anything a customer sees. The dashboard does not know the caller exists.
- **First proof:** a client's voice receptionist, measured at 45% of test calls handled (20 of 44), target 95%. A separate developer is fixing the agent. This repository only measures it.
- **Live runs so far:** 19 calls against the test agent over the direct connection. None over a phone line. The 7 booking calls reached no booking: four causes were the rig's or the fixture's, one was the agent mishearing.

Read before starting: the five audits in `docs/audits/2026-11-25/` (call path, security and tenancy, rig validity, product readiness, testing-product plan) and `docs/ROADMAP-2027.md`.

## 2. Rules for everyone

1. **One package, one branch, one pull request.** Branch from `origin/main`. Rebase before every push.
2. **Push early.** Push work in progress to the remote after every meaningful step and keep a "Remaining" checklist in the pull request body. Work that exists only on one machine is lost when that session stops.
3. **Pull requests do not sit in draft.** Mark ready as soon as CI is green and the work is complete. The owner merges, with a merge commit (never squash).
4. **CI must be green on all four jobs:** Backend, Frontend, Caller, Dependency audit.
5. **Failing-first tests.** A fix ships with a test that failed before it. No skipped tests, no tests that assert nothing.
6. **No live calls from tests.** Tests use the fake leg and mock transports.
7. **Live calls go only to the test agent** (`agent_2101m2x7asq2e58vs1awy7r0kgmh`) and its test clinic. Never the client's agent. Keep the mutation guard on; open it only with the explicit allow flag.
8. **Never print or commit a key.** Read keys from the environment at run time.
9. **Send no owner, client or clinic names to any outside service** (searches, model prompts to third parties, vendor forms).
10. **Do not upload or clone any real person's recorded voice.** Synthetic stand-in clips live in a separate bank and are labelled synthetic in every result.
11. **Say what was not verified.** Every pull request body ends with a "Not verified" list.
12. **Workflow files:** do not add a bare `runs-on: ubuntu-latest` line; reuse the existing runner expression.

## 3. In-flight packages (finish these first)

Each has a branch on the remote. Check its pull request's "Remaining" list, rebase, finish, get CI green.

| # | Branch | Goal | Done when |
|---|---|---|---|
| F1 | `fix/caller-measurement-validity` | A pass is a true pass and a fail a true fail (section 4) | Every item in section 4 has a failing-first test and passes; all booking scenarios state what must be booked |
| F2 | `fix/caller-trace-and-scorecard` (PR #28) | Full tool results in the ElevenLabs trace; honest prerequisite checks; clip override file; HTML scorecard | A lookup call shows found / not found / near match from the tool result body; `scorecard` renders one self-contained page |
| F3 | `feat/caller-phone-in-batch` | Phone calls in the batch runner, graded like direct calls, each side recorded | `batch --leg phone` runs with fakes; trace lookup fails to "ungraded", never to a pass; `preflight` refuses a number not on the allow-list |
| F4 | `feat/caller-runs-in-dashboard` | A signed-in user can upload a run and see it | Upload API with ownership checks; list, run and call pages; cross-workspace denial tested |
| F5 | `fix/tenant-isolation-and-signup` | One customer cannot reach another's data | The 7 routes from the security audit are fixed with denial tests; sign-up closable; no usable default credentials |
| F6 | `fix/secrets-at-rest-audit-erasure` | Stored keys encrypted; audit trail; export and erasure work | Round-trip, rotation and wrong-key tests; export and erasure pass on Postgres; status endpoint reports recorded runs |
| F7 | `fix/dashboard-dead-ends-and-quick-start` | No dead controls; a quick start that works | Test setup fails on unmocked requests; README steps followed against the repo; migration-drift check in CI |

Merge order when several are ready: F2, F1, F3 (all touch `caller/` `cli.py`, `batch.py`, `report.py`), then F5, F6, F4, F7 (F4 to F7 touch `main.py` and the dashboard navigation; conflicts are one line each).

## 4. What "measurement validity" means (package F1)

From the rig-validity audit and the live booking run. All are confirmed in code.

1. A booking passes only when the booked values match the scenario: day or date window, time window, appointment type, clinician where stated, identity fields.
2. A tool's outcome is read from its result body, per tool. A transport-level `success: true` is not an outcome.
3. Unanswered calls, early deaths and time-limit exits are outcomes with a fault class (agent, rig, fixture, unknown). They are never dropped. A call that hit the time limit cannot pass.
4. The headline rate is reported both "of graded" and "of attempted", with every exclusion listed.
5. When no step matches the agent's line, the result is "rig could not answer", not an agent failure.
6. Several steps may match one agent turn (a combined question gets all its answers).
7. Clips without a human-confirmed label are usable only with an explicit flag, and results then say "labels unverified".
8. Identity attempts are a target setting (default 2), and runs that would exceed it are spaced or refused.
9. A withheld tool result is a third state: "unavailable", which makes dependent expectations "ungradeable".
10. Repeats of one scenario are not independent: report the interval pooled and clustered.
11. A value sent to a tool but not read back is "sent, not confirmed stored".

**Acceptance gate.** 58 of 60 demonstrates about 90%, not 95%. To demonstrate 95% at one-sided 95% confidence: 59 valid calls with no failures, or 124 with at most two. A genuinely 95% agent still fails a 20-call test at 90% about 8% of the time; say so to the client.

## 5. Queued packages

Ordered. Q1 to Q6 serve the client proof; the rest serve a first outside pilot. Sizes are rough.

| # | Package | Depends on | Size | Done when |
|---|---|---|---|---|
| Q1 | **Re-run the booking scenarios** against the test clinic (section 6 runbook) | F1, F2 merged | half a day | Each scenario run twice; stored rows checked; clinic left clean; scorecard produced |
| Q2 | **First real phone call** (section 7 runbook) | F3 merged; owner's three approvals | 1 day | One lookup-only call placed, both sides recorded, graded from the trace |
| Q3 | **Wire reset and read-back to the test clinic** | F1 | 2 days | A config file plus a small wrapper (outside `caller/src`) that calls the product's fixture commands and a read-only lookup; nothing clinic-specific inside the package |
| Q4 | **Evidence per call:** record both sides on the direct connection too; a call page with audio playback and a timeline | F3, F4 | 4 days | A reviewer can listen to any call and see which clip was played at which moment |
| Q5 | **Acceptance report:** dated export with intervals, fault classes, rig and agent versions, the gate result | F1, F2 | 3 days | One file a client can be sent; reproducible from the stored run |
| Q6 | **Daily scorecard** for the client proof as each agent fix lands | Q1 | ongoing | Scorecard compared with the previous one; regressions named |
| Q7 | **Call-bridge quick fixes:** end the model session on hang-up; remove the five-minute cap; make transfer and keypad tools real or remove them | F5 merged | 3 days | Tests for each; see the call-path audit, findings 1 to 3 |
| Q8 | **Account lifecycle:** password reset, session refresh, multi-user workspaces | F5 | 4 days | Journey tests for each |
| Q9 | **Usage metering and limits** on test endpoints and caller runs | F4 | 3 days | Per-workspace counts; a limit that refuses politely |
| Q10 | **Capture a live call into a test:** `ingest` and `capture` commands (audio, trace, transcript in one step) | F2 | 4 days | A failed call becomes a scenario skeleton with its clips cut and marked "unverified" |
| Q11 | **Clip bank consent and provenance:** per-clip source, lawful basis, consent record, retention date; `forget` that propagates to suites and results | F1 | 3 days | Deleting a clip removes it everywhere and leaves a record that it was removed |
| Q12 | **Volunteer clip bank:** consenting volunteers reading fictional details, UK accents | Q11 | 2 days plus recording | A bank usable for outside sales with no real caller audio |
| Q13 | **Onboarding kit:** fetch and transcribe a customer's recordings, region option, scenario reference | Q10 | 3 days | A new target can be set up from a document |
| Q14 | **Scheduled runs and a CI gate** using `--fail-under` and `compare` | F3 | 2 days | A nightly run that fails loudly on regression |
| Q15 | **Remaining call-bridge findings** | decision in section 8 | — | Become requirements and tests for the new engine |

## 6. Runbook: live runs against the test clinic

The product repository (not this one) ships fixture commands for the test clinic. Run them from that repository with Node 24.

1. Print the plan (reads only): `npm run voice:fixture`
2. Seed the fixture appointment: `npm run voice:fixture -- seed --apply --confirm-host <host the script prints>`
3. Run the batch with the mutation guard opened for the test target only (`--allow-mutating books`), sequentially.
4. After each call that changes data: `npm run voice:fixture -- adopt --conversation <conversation id>`
5. At the end: `npm run voice:fixture -- restore --apply --confirm-host <host>` and `npm run voice:fixture-patients -- purge --apply --confirm-host <host>`. Print the plan first; stop on any "REFUSED".
6. Grade "stored" from a read-only lookup of the row, not from the tool's reply.
7. List anything left behind, row by row.

Known facts about the test clinic:
- It has several practitioners, so the agent asks "which doctor". The real clip bank has no answer to that. Use the synthetic stand-in (`practitioner_no_preference`) and label it.
- Identity lookups lock after 2 attempts per patient for 900 seconds, across calls. Space the calls.
- The clip bank speaks identities that do not exist in the clinic. Run the new-patient registration scenarios first so later scenarios find those patients. Do not insert patients by hand.
- 15 older test registrations are not in the ownership ledger. Leave them.

## 7. Runbook: first real phone call

Needs the owner's go-ahead for each of the first three steps. Do none of them without it.

1. Attach a spare UK number to the test agent in the agent platform (the test agent has no number today).
2. Use the unused Twilio number as the caller's number and point its voice webhook at the rig.
3. Open a temporary public tunnel to the rig's media websocket.
4. Run `preflight` for the phone leg. It must pass, and the target number must be on the allow-list.
5. Place one lookup-only scenario. Record both sides.
6. Grade from the agent's trace, found by called number and start time.
7. Compare what was heard on the phone path with the same clip over the direct connection. The open question is whether the phone path reproduces a misheard date that the direct connection did not.

Note: Twilio media streams run in the US, Ireland or Australia. Fine for testing; it cannot support a "UK-only" claim.

## 8. Decisions that are the owner's

1. **The inherited call bridge.** Seven findings block a paying deployment. Recommendation: fix three now (Q7), treat the rest as requirements for the new engine. Not decided.
2. **The old text test harness.** It tests a different model from the one real calls use. Recommendation: label it plainly now (F7), retire it once the caller is in the dashboard. Not decided.
3. **Engine base.** Pipecat or LiveKit Agents, decided by a side-by-side on the same clips after the client proof. Not started.
4. **A real recording for "which doctor".** A two-second clip from a consenting person replaces the synthetic stand-in.

## 9. Not ours to do in this repository

- Fixing the client's agent. That is the agent developer's six steps.
- The check of a bought testing tool's audio replay. That runs in its own session.
- Anything in the client's clinic: its clinician's hours, calendar or bookings.

## 10. Choosing a model for a package

- **Any capable coding model can take:** F4, F7, Q8, Q9, Q13, Q14. They are ordinary web and API work with clear tests.
- **Take more care with:** F1, F5, F6, Q3, Q11. A mistake here produces a false pass, a data leak or lost data. Have a second model or a person review the diff before it is marked ready.
- **Need a person present:** Q1, Q2, Q12. They place live calls or record people.

## 11. Where things are

- Audits: `docs/audits/2026-11-25/`
- Roadmap: `docs/ROADMAP-2027.md`
- Caller docs: `caller/README.md`, `caller/docs/` (phone leg, adapters, reset and chains)
- Deployment and settings: `DEPLOYMENT.md`, `docs/DECISIONS.md`
- Recordings, clip banks and run outputs are kept outside the repository on the owner's machine, because they contain a real person's recorded voice.
