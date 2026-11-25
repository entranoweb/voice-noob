# Synthiq Voice: work packages for other developers (25 November 2026)

Each brief below is complete on its own. Hand one brief to one developer (or one model session) together with `docs/plans/2026-11-25-continuation-plan.md`, whose section 2 holds the rules that apply to every package. Nothing here needs the session that wrote it.

Repository: `entranoweb/voice-noob`. Branch from `origin/main`. One package, one branch, one pull request.

## Before you start any package

1. Read section 2 of the continuation plan (twelve rules). The three that cause the most damage when skipped:
   - Push work in progress to the remote after every meaningful step, and keep a "Remaining" checklist in the pull request body.
   - No live calls from tests. Live calls go only to the test agent, with the mutation guard on.
   - A pull request that adds a database migration must be the only such pull request merged at a time. Set `down_revision` to the current head on `main`, and check `alembic heads` shows one head.
2. Read the audit that covers your area in `docs/audits/2026-11-25/`.
3. Run the suite for your area before changing anything, so you know what was already failing: `caller/` (pytest, ruff, mypy), `backend/` (pytest on Postgres through compose), `frontend/` (lint, types, tests).

## Packages

Sizes are estimates. "Care" means a mistake produces a false pass, a data leak or lost data, so a second reviewer reads the diff before it is marked ready.

| ID | Package | Size | Care | Needs first |
|---|---|---|---|---|
| P1 | Wire reset and read-back to the test clinic | 2 days | yes | — |
| P2 | Evidence per call: recordings and playback | 4 days | no | #29, #32 merged |
| P3 | Acceptance report | 3 days | yes | — |
| P4 | Call-bridge quick fixes | 3 days | yes | #33 merged |
| P5 | Account lifecycle | 4 days | yes | #33 merged |
| P6 | Usage metering and limits | 3 days | no | #32 merged |
| P7 | Capture a live call into a test | 4 days | no | — |
| P8 | Clip bank consent and provenance | 3 days | yes | — |
| P9 | Onboarding kit | 3 days | no | P7 |
| P10 | Scheduled runs and a CI gate | 2 days | no | #29 merged |
| P11 | Run manifest and reproducibility | 2 days | no | — |
| P12 | Scorecard and dashboard show the new outcome classes | 1 day | no | #32 merged |
| P13 | Engine comparison harness | 5 days | no | after the client proof |

---

### P1. Wire reset and read-back to the test clinic

**Why.** The caller has generic reset and read-back hooks (`caller/src/synthiq_caller/reset.py`, `readback.py`). They have never been pointed at a real system. Until they are, "stored" is graded by hand.

**Goal.** A configuration, kept outside `caller/src`, that makes a batch against the test clinic reset itself before scenarios that need a clean state and read the stored row after each call.

**Start from.** `caller/docs/RESET-READ-BACK-CHAINS.md`; section 6 of the continuation plan (the product's fixture commands: `voice:fixture` seed, adopt, restore; `voice:fixture-patients` purge).

**Build.**
1. A target config file (for example `caller/targets/test-clinic.example.toml`) holding: reset command, after-call hook command (the adopt step, which needs the conversation id the hook now receives), read-back command, identity attempt budget, allow-flag for mutation.
2. A small wrapper script under `caller/scripts/` that the read-back hook calls. It takes the scenario's captured identifiers and returns the stored patient or appointment as JSON. It must be read-only and scoped to the one test clinic. Connection details come from the environment.
3. Nothing clinic-specific inside `caller/src`.

**Done when.** With fakes, a batch run shows reset before a `clean_state` scenario, the adopt hook after a mutating call, and `stored_source: read back` on graded fields. A failed reset marks the call "not run". A dry-run mode prints every command it would run.

**Do not.** Run it against anything live as part of the pull request. Insert or delete rows by hand anywhere.

---

### P2. Evidence per call: recordings and playback

**Why.** A client cannot be asked to trust a pass rate without hearing a call. The phone leg records each side (#29); the direct connection records nothing; the dashboard shows no audio.

**Goal.** Every call, on either leg, has both sides saved, and the dashboard's call page plays them against the timeline.

**Build.**
1. Direct connection: save the agent's audio and the caller's played audio as two WAV files per call, referenced from the run file (same fields the phone leg uses in `recording.py`).
2. Upload: `synthiq-caller upload` sends the audio with the run. Backend stores it per workspace (follow how the backend already stores call recordings; if it has no file store, add one behind an interface with a local-disk implementation) with size caps and a content-type check.
3. Call page: a player with the two tracks, and timeline markers for "caller clip played", "agent spoke", "tool call". Clicking a timeline row seeks the audio.
4. Deletion: deleting a run deletes its audio. Erasure (see `app/services` data-protection code from #30) covers it.

**Done when.** Component tests for the player and markers; backend tests for upload, ownership (another workspace gets 404 on the audio), size cap and deletion.

**Not expected.** A browser run. Say so.

---

### P3. Acceptance report

**Why.** The deliverable to a client is a dated statement of what was tested and what passed.

**Goal.** `synthiq-caller acceptance <run dirs> --claim 0.95 --out report.html` producing one self-contained file.

**Contents.** Target label and version; rig version and commit; scenario set hash; clip bank identity and whether labels were human-confirmed; which clips were synthetic; calls attempted, graded, passed; every excluded call with its reason; rate "of graded" and "of attempted" with pooled and by-scenario intervals; the `gate` result for the claim (calls needed, calls made, met or not met); failures grouped by fault class with last step; said, heard, stored disagreements per field with `stored_source`; a plain statement of what the run does not show (for example: direct connection, not a phone line).

**Start from.** `report.py`, `compare.py`, the `scorecard` and `gate` commands.

**Done when.** Tests cover: a run that meets the gate; one that fails it; one with too few calls ("not enough calls to support the claim", never "passed"); unverified labels and synthetic clips appear on the page. The same inputs always produce the same file apart from the generation time.

**Care.** The report must never round in the client's disfavour or ours. State counts, not only percentages.

---

### P4. Call-bridge quick fixes

**Why.** Call-path audit, findings 1 to 3. These would embarrass any demo.

**Goal.**
1. When either side of the bridge ends, end the other: close the model session, release the database connection, remove the registry entry, write the transcript. (`backend/app/api/telephony_ws.py`, both the Twilio and Telnyx bridges.)
2. Remove the hard five-minute cap; replace with a configurable maximum (default 30 minutes) and an idle timeout.
3. `transfer_call` and `send_dtmf` (`backend/app/services/tools/call_control_tools.py`): make them act through the carrier, or remove them from the tool list and say so. A tool must never report success for something that did not happen.

**Done when.** Tests with fake sockets show: caller hang-up closes the model session within a second and the transcript is saved; a model-side close ends the carrier leg; a provider `error` event ends the call and is logged as an error; a long call is not cut at 300 seconds.

**Do not.** Rewrite the bridge. The roadmap replaces it; the other audit findings are requirements for the replacement.

---

### P5. Account lifecycle

**Why.** Product-readiness audit, finding 5. Sign-up is now closed by default and accounts can be disabled (#33); the rest is missing.

**Goal.** Password reset by emailed single-use link (token hashed at rest, 30-minute life, all sessions revoked on reset); refresh tokens with rotation and reuse detection so a user is not signed out every 30 minutes; invitations so a workspace can have more than one member, with owner and member roles; every one of these written to the audit trail.

**Done when.** Journey tests: reset happy path, expired link, reused link; refresh rotation and a stolen-token replay being refused; invite, accept, member can see workspace data, removed member cannot.

**Care.** Every existing ownership check assumes one user per workspace. Find them all (`grep` for `user_id ==` in `backend/app/api`) and decide each: owner-only or any member. List the decisions in the pull request.

---

### P6. Usage metering and limits

**Goal.** Count per workspace: caller runs uploaded, calls in them, test minutes, stored audio bytes, text-harness runs. A limits table with defaults; a polite refusal (HTTP 429 with a message naming the limit) when exceeded; a usage page in settings.

**Done when.** Tests for counting, for the refusal, and for limits being per workspace. No billing provider integration.

---

### P7. Capture a live call into a test

**Why.** The first half of the self-improving loop: a failed real call becomes a reproducible test.

**Goal.** `synthiq-caller capture --trace elevenlabs --conversation <id> --out cases/<name>/` fetching the audio, the trace and the transcript in one step; then `synthiq-caller ingest cases/<name>/` cutting the caller's turns into clips (reuse `clipbank/build.py`), writing a manifest with every clip marked "label unverified", and writing a scenario skeleton whose steps are the agent's questions in order and whose expectations are left empty for a person to fill.

**Done when.** Tests on a recorded fixture produce a scenario that `validate` accepts as "ungradeable until expectations are stated". Nothing is uploaded anywhere.

**Care.** Do not transcribe with, or send audio to, any service the user has not configured. Never treat a machine transcript as the truth of what was said; that is why the label is "unverified".

---

### P8. Clip bank consent and provenance

**Why.** A clip is a recording of a person. Nothing can be sold on a clip bank that cannot show where each clip came from or delete it properly.

**Goal.** Manifest fields per clip: source (call or recording session), speaker reference (an opaque id, never a name), provenance (real or synthetic), lawful basis, consent record reference, date recorded, retention date, human-confirmed label (who and when). A `forget <speaker or clip>` command that removes the audio, removes the clip from every scenario that uses it (the scenario becomes "missing clip"), marks every stored result that used it, and writes a record that the removal happened. `validate` refuses clips past their retention date.

**Done when.** Tests for each of those, and for a batch refusing a clip with no provenance unless explicitly allowed.

**Do not.** Add voice cloning, speaker matching or any voice embedding.

---

### P9. Onboarding kit

**Goal.** A document plus commands that let a new target be set up without us: connect the platform's key, pick the region, fetch a sample of recent calls (P7), choose a trace adapter, write a first three scenarios from a reference, run `validate`, run one lookup-only call. A scenario reference page generated from the scenario schema.

**Done when.** A second person follows it on a clean machine against the fake leg and reaches a graded run.

---

### P10. Scheduled runs and a CI gate

**Goal.** A workflow that runs a named scenario set on a schedule or on demand, compares with the stored baseline (`compare`), fails with `--fail-under`, uploads the run to the dashboard and attaches the scorecard. A documented example for a customer's own CI.

**Care.** The workflow must not place mutating calls and must not run against any target not on an allow-list held in repository settings. Do not add a bare `runs-on: ubuntu-latest` line; reuse the existing runner expression.

---

### P11. Run manifest and reproducibility

**Why.** Rig-validity audit: a result cannot today be tied to the exact rig, scenarios, clips and agent version that produced it.

**Goal.** Every run directory gets a manifest: rig commit and version, scenario file hashes, clip bank hash and overrides hash, target identifier and its reported version or configuration hash where the platform exposes one, start and end times, the command line, and the random seed if scenario order is shuffled. `regrade` and `acceptance` refuse to mix runs with different scenario hashes unless told to.

---

### P12. Scorecard and dashboard show the new outcome classes

**Goal.** The scorecard page and the dashboard run page display failed, void (rig could not answer), ungradeable, not run and unavailable as separate rows with counts and reasons, the "of attempted" rate beside "of graded", and synthetic-clip and unverified-label marks. The data already carries all of it.

---

### P13. Engine comparison harness (after the client proof)

**Goal.** Two minimal receptionist agents with the same code-decided flow, one on Pipecat Flows and one on LiveKit Agents, each reachable by the caller's direct leg and phone leg. Run the same clip-driven scenarios against both. Measure false cut-offs on spelled postcodes, time to first audible reply, and stored-field accuracy. Include a regression scenario for a caller who talks over the greeting.

**Not a product build.** It exists to choose the base for the new engine with numbers.

## What the owner must supply for end-to-end testing

- Go-ahead to attach a spare number to the test agent, to place calls from the unused carrier number, and to open a temporary tunnel.
- A speech-recognition key if the rig's own transcript on the phone leg is wanted (optional).
- A person to listen to the flagged clips and confirm labels.
- A two-second real recording for "which doctor" if the synthetic stand-in is not acceptable.
