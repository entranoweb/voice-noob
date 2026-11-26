# Synthiq Voice: the testing product — where it stands and what to build next

Read-only study, 25 November 2026. Code read at `entranoweb/voice-noob` main `56702fa` (after #24), plus the description and file list of open PR #25. Competitor columns come from public docs and changelogs read today; nothing was tested hands-on. Anything marked *inference* is mine.

## The answer

The caller is a sound instrument with a narrow, real edge and almost no product around it. Three facts shape the order of work:

1. **There are two testing products in the repository and they do not touch.** The dashboard's testing pages run the older text harness (`backend/app/services/qa/`, about 8,000 lines, routes in `backend/app/api/testing.py` and `backend/app/api/qa.py`). The new `caller/` package is a command-line tool. No file in `backend/`, `frontend/` or `.github/` imports or invokes `synthiq_caller`. A buyer who logs in today cannot see, run or read a caller result.
2. **Our "stored" is not yet the stored record on main.** `grading.py:209-217` takes "stored" from the arguments the agent's tool *received*. PR #25 adds a real read-back with a `stored_source` label. Until that is merged and wired to a real system, our headline claim is one step short of true. Roark already documents the nearest competing feature (API Checks: their platform asks the customer's backend for a pass/fail per call).
3. **Competitors are far ahead on everything that is not the edge:** platforms connected, synthetic personas, monitoring of live calls, scheduling, CI apps, human review, MCP servers, and prompt-fixing loops. Chasing those is a losing race. The plan below builds the minimum product shell and spends the rest on trust in a result.

## 1. Capability matrix

Synthiq column: **W** working (run live), **B** built, not run live, **S** stub or partial, **A** absent. Competitor cells: **Y** documented, **P** partial or unclear, **–** not found in the pages read. "Not found" is not proof of absence. Hamming's docs are behind a login, so its column is marketing pages only and is marked †.

| Need | Synthiq today | Cekura | Hamming† | Coval | Bluejay | Roark | Nopaque |
|---|---|---|---|---|---|---|---|
| **Authoring: by hand** | W — JSON scenarios, `caller/src/synthiq_caller/scenario.py:105`; 20 in `caller/scenarios/example-receptionist/` | Y | Y | Y | Y | Y | Y (saved discovered paths) |
| Authoring: from a production call | S — `clipbank/from_call.py:1-7` lays clips out as steps; a person still sets facts, expectations and `mutates` | Y ("create simulations from production calls", Nov 2025) | Y† | – (upload and score, not convert) | Y (Replay From Transcript) | – | P (mapping by calling the number) |
| Authoring: generated | A in `caller/`; S in the text harness (`backend/app/services/qa/scenarios.py`, seeded set) | Y (also from the agent's code, Oct 2026) | Y† | Y | Y | Y | Y |
| **Realism: real recorded clips as caller input** | W — `clips.py`, `director.py:208` | Y per changelog (Aug and Sep 2026); contradicted by its FAQ; unverified | Claimed† | – | – | – | – |
| Realism: clip chosen from what the agent just said | W — regex on agent line, `director.py:164-206` | P ("pre-recorded clip to a conditional action") | – | – | – | – | – |
| Realism: synthetic voice, personas | A in `caller/`; text-only LLM caller in `backend/app/services/qa/caller.py:1-19` | Y | Y† | Y | Y | Y | P |
| Realism: accents | A (whatever the recorded speaker has) | – | – | Y | – | Y | – |
| Realism: background noise | A | Y | – | Y | P (noise upload API) | Y | – |
| Realism: interruption, silence, late answer | B — `talk_over`, `stay_silent`, `delay_s`, `scenario.py:49-51`; never used in a live run | Y | – | Y | P | Y | – |
| Realism: keypad digits | B — in-band tones on the phone leg `dtmf.py`; socket message in PR #25, untested live | Y | – | – | Y (IVR) | – | Y |
| **Transport: direct socket** | W — ElevenLabs only, `legs/elevenlabs_socket.py`; the only choice the CLI offers, `cli.py:198,212` | Y | – | Y | Y | – | – |
| Transport: phone (PSTN) | B — `legs/twilio_media.py`, `phone_server.py`; one scenario at a time, not in `batch`; no call placed | Y | Y† | Y | Y | Y | Y |
| Transport: SIP | A | Y | – | – | Y | – | – |
| Transport: WebRTC | A | Y | – | Y (LiveKit) | – | Y | – |
| **Platforms and trace adapters** | 1 working (`traces/elevenlabs.py`); Retell and Vapi in flight, doc-derived | ElevenLabs, Vapi, Retell, Bland, LiveKit, Pipecat, Synthflow, Sierra, others | several† | OpenAI Realtime, Gemini Live, Pipecat, LiveKit, phone | 13 named | several | any phone number |
| **Grading: deterministic from tool calls** | W — `grading.py:254-265` | Y (tool call metrics) | P† | Y (trace metrics) | Y (deterministic metrics) | Y (tool call accuracy) | – |
| Grading: per field said / heard / stored | W — `grading.py:220-243`, but "stored" = tool argument | – | – | – | – | – | – |
| Grading: stored-record read-back | B in PR #25 (`readback.py`); A on main | – | P† (fixture guide) | P (SQL metrics on own data) | P (inside G2 benchmark only) | **Y — API Checks: one pass/fail per call from the customer's endpoint** | – |
| Grading: LLM judge | A in `caller/`; W in text harness (`evaluator.py`) | Y | Y† | Y | Y | Y | Y (mission outcome) |
| Grading: audio metrics (latency, talk-over, silence) | A — the timeline has times (`director.py:98`) but nothing computes latency; text harness has `metrics/diagnostic/response_speed.py`, `metrics/experience/turn_taking.py` on its own calls | P (silence, pronunciation, tone; latency not found) | Y† | Y (statistical metrics) | P | Y (Live Bench) | P (timing diffs) |
| Grading: human review queue | A | Y ("Review Required", overrides) | Y† | Y | Y (Metrics Lab) | – | – |
| **State: reset** | S — shell hooks `batch.py:61-103`, `cli.py:225-226`; typed reset in PR #25; no real endpoint wired | P (mock tools swapped and restored) | P† | P (pre-call data fetch) | P (mock tools) | P (mock tools) | – |
| State: chains with captured values | B in PR #25 (`chain.py`) | – | – | – | P — Customer Journeys runs steps as one identity; value passing and backend checks not documented | – | – |
| State: transactional fixtures with verified rollback | W for our own agents only — `backend/app/services/qa/fixtures.py:1-22`, `metrics/validation/state_restored.py` | – | – | – | – | – | – |
| **Scale: parallel calls** | B — `--max-parallel`, `cli.py:216` | Y | Y† | Y | Y (load testing) | Y | Y |
| Scale: scheduling | A | Y ("Monitors") | Y† | Y | Y (API) | Y | Y |
| Scale: CI gate | A for the caller (our own CI only tests the package, `.github/workflows/ci.yml:189`) | Y (GitHub app, tests as code) | Y† | Y (GitHub Actions) | Y (GitHub, GitLab, Azure DevOps) | Y | Y |
| Scale: regression against a baseline | B in PR #25 (`compare.py`, Wilson intervals) | P | P† | Y (compare runs) | Y (cookbook) | Y | Y ("scored against the last green run") |
| **Monitoring: ingest live calls** | A in `caller/`; S in backend for our own agents (`evaluator.py`, `alerts.py`) | Y | Y† | Y (upload) | Y | Y | – |
| Monitoring: failed live call becomes a test | S — manual: `clipbank build` then `clipbank scenario` | Y | Y† | – | Y | – | – |
| **Results: scorecard** | S — text table `report.py:101`; HTML scorecard in flight | Y | Y† | Y | Y | Y | Y (signed reports) |
| Results: per-field evidence | W — `report.py:14-21` | – | – | – | – | – | – |
| Results: audio playback with timeline | A — no call audio is saved on the socket leg; phone leg can ask Twilio for a dual recording, `twilio_rest.py:145-146` | Y | Y† | Y | Y | Y | P |
| Results: export | S — JSON run files | Y | Y† | Y | Y | Y | Y (dated, hashed PDF) |
| **Team: workspaces, sharing** | W in the dashboard for the text harness; A for the caller | Y | Y† | Y | Y (SSO, SCIM) | Y | Y |
| Team: API / SDK / CLI | CLI only | Y (CLI, Python SDK) | Y† | Y | Y | Y | Y |
| Team: MCP server | A | Y | – | Y | Y | Y | Y (88 tools) |
| **Compliance: consent and provenance per clip** | A — manifest has clip, call, category only (`clipbank/build.py:27-30`); no consent, speaker or basis field | – | – | – | – | – | – |
| Compliance: redaction | A | Y | Y† | – | Y | Y | – |
| Compliance: deletion that propagates | A | – | – | – | P (delete endpoints) | P (retention) | – |
| Compliance: UK storage | A (nothing is hosted) | P (EU deployment, on-prem observability) | P† | – | – | – | P (UK company; location not stated) |
| Compliance: evidence export for a regulator | A | – | – | – | – | – | **Y — EU AI Act Art 50, FCA, Ofcom; none for health** |

Sources read today: Cekura changelog (https://www.cekura.ai/changelog); Bluejay docs index, Customer Journeys and Self-Improve pages (https://docs.getbluejay.ai/llms.txt); Coval docs index (https://docs.coval.ai/llms.txt); Roark docs index and API Checks page (https://docs.roark.ai/llms.txt, https://docs.roark.ai/documentation/metrics/custom-metrics/api-checks.md); Nopaque home page (https://www.nopaque.co.uk). Hamming docs returned a login page.

### Open implementations worth learning from

| Repository and path | What to take |
|---|---|
| `openai/openai-cookbook`, `examples/audio/duplex_voice_agent_evaluation/` (`crawl_harness`, `walk_harness`, `run_harness`, `shared/audio/effects.py`, `shared/reporting/results.py`, `run_harness/visualization/export_viewer.py`) | A three-rung ladder (text → recorded audio single turn → simulated multi-turn) with one versioned scenario contract across all three; a named set of audio conditions applied to recorded clips; separate audio and task metrics; an exported viewer. Our clips could get noise and line conditions the same way without new recordings. |
| `openai/openai-cookbook`, `examples/evals/realtime_evals/walk_harness/run_realtime_evals.py` | "Replay saved G.711 mu-law WAV audio deterministically with fixed chunking and manual turn commits (VAD off) to keep runs comparable": the argument for fixing chunk size and recording versions so runs are comparable. |
| `livekit/agents`, `tests/test_workflows.py`, `tests/test_start_capture_run.py` | `session.run(user_input=...)` returning a `RunResult` with `result.expect.next_event()`: an ordered-event assertion style. Our `expect` is unordered; an ordered form would catch "tool called before the caller confirmed". |
| G2/Bluejay methodology (PDF cited in the research pack) | Per-call fresh store, frozen at hang-up, diffed: the published precedent for stored-record grading, and the benchmark any claim of ours will be compared with. |

## 2. The self-improving loop, on our code

Every competitor's loop ends by rewriting a prompt and re-running a judge until a score rises (Bluejay Self-Improve: one failure pattern per round, keep the new prompt if the pass rate rises by 0.02, push to the live agent; Cekura: propose a fix, re-run "until they pass", optionally as a pull request). Ours should differ at the two ends: it starts from the caller's real audio, and it accepts a fix only on stored records. The middle can be plain.

| Step | Exists today | Missing | Smallest build | Human stays in? |
|---|---|---|---|---|
| 1. A live call fails | Nothing detects it. Failures were found by hand in the root-cause work. Backend `evaluator.py` and `alerts.py` judge only our own agents' calls. | A feed of the target platform's calls and a deterministic failure rule | `ingest` command: list conversations for an agent since a time, run the trace adapter (`traces/elevenlabs.py`), mark a call failed when a mutating tool was refused, the call ended at a dead-end step, or a stored field disagrees with a later correction. No judge model. | No, for detection. Yes, to confirm a failure is real before it becomes a test. |
| 2. Captured | Done by hand: recording download plus batch transcript with word times and speakers (the input shape `clipbank/build.py:3-8` expects) | One command that fetches audio, trace and a word-timed transcript for a call id | `capture <call-id>` writing `<call>.mp3`, `<call>.stt.json`, `<call>.trace.json` to a folder outside any repository | No |
| 3. Clipped and labelled | `clipbank build` cuts each caller answer (`clipbank/build.py`, `segments.py`), labels category and value (`values.py`), flags doubtful clips | Consent and provenance: the manifest has no speaker, consent, lawful-basis, source-controller or expiry column. No per-clip delete. | Add those columns; refuse to use a clip in a batch unless `consent` is set; `clipbank forget <speaker or call>` that deletes clips and lists every scenario and result that used them | **Yes.** A person confirms consent and listens to flagged clips. This cannot be automated. |
| 4. Becomes a scenario | `clipbank scenario` (`clipbank/from_call.py`) lays clips out as steps keyed on the question each answered | Facts, expectations, `mutates` and `requires` are left blank; the `WHEN` table (`from_call.py:18-40`) is written for one agent's wording | Fill `facts` from the labelled values; propose `expect` from the failed call's trace (the tool that should have succeeded, the field that was stored wrongly); move the `WHEN` table to a per-agent file | **Yes.** A person approves the expectation: it is the definition of "fixed". |
| 5. Reproduced N times | `batch --repeats` (`cli.py:215`), guards (`batch.py:106-133`, `--allow-mutating` `cli.py:217`) | A rule for "reproduced": today a person reads the report | `reproduce` = run N (default 5); reproduced if the same check fails in at least 2. Record the rate with its interval (`stats.py`). A failure that never reproduces is filed as "not reproduced on this leg", which is itself a finding (the date misheard on the phone but not the socket). | No |
| 6. Fault classified | Free text in run notes. `report.py:42-55` gives the failing step. | A recorded class per failure | Rules first: reset failed or prerequisite unmet → fixture; `no_step` or clip missing → rig or scenario; heard ≠ said → hearing; heard = said but stored ≠ said → agent logic; tool refused → system under test. Anything else → "unclassified", sent to a person. | Yes, for "unclassified" only |
| 7. Fix proposed | Nothing | — | **Do not build an automatic fixer now.** Emit a fix brief: the failing step, the evidence rows, the class, three similar past failures. A developer or a coding agent writes the change. *(Inference: for agents built as code-decided flows, the fix is code, not a prompt, and a prompt-rewriting loop does not apply.)* | **Yes.** A person owns the change. |
| 8. Re-run against baseline | PR #25: `baseline`, `compare` with Wilson intervals | Merge; recording of agent version and rig version in each run file | Stamp every run with agent version id, rig commit, clip-bank hash, leg; `compare` refuses to compare runs with different clip banks without a flag | No |
| 9. Accepted | Nothing | The rule | Accept when: the new scenario passes k of n with the interval's lower bound above the agreed floor; **no** baseline scenario regresses beyond noise; every pass has `stored_source = read back` for mutating scenarios; zero residue after reset. Then the scenario joins the permanent set. | Yes: a person signs the acceptance; the tool produces the record they sign. |

Missing end to end: steps 1, 2, 6, 7 (as a brief), 9, and the consent half of 3. Steps 3 (cutting), 4 (skeleton), 5 and 8 exist or are in PR #25.

## 3. Ordered plan: the next 12 pieces of work

Sizes are working days for one developer and are *inference*. "Proof" = helps the current client proof (the agent to 95%).

| # | Work | What it unlocks for a buyer | Days | Depends on | Proof or product |
|---|---|---|---|---|---|
| 1 | **Stored-record grading made real.** Merge PR #25; wire read-back and reset to one real system; every mutating pass must show `stored_source = read back`; report "tool said yes, record says no" as its own failure class | The one claim nobody else makes per field. Without it we are a transcript grader with better inputs. | 3 | PR #25; a test system with a lookup | Both |
| 2 | **Evidence per call: recorded audio, timeline, versions.** Save both sides of each call (socket: write the PCM we send and receive; phone: dual recording, `twilio_rest.py:145`); stamp agent version, rig commit, clip-bank hash; one page per call with audio, timeline, tool calls and field rows | A sceptical client can listen to any pass or fail. Today a result is a JSON file. | 4 | Rig-fix work in flight (full tool results) | Both |
| 3 | **Phone leg run live and put into `batch`.** First real call; phone leg selectable in `run` and `batch` (`cli.py:198,212` offers only the socket); rig's own transcript beside the agent's | Tests what the customer's callers actually use, on any platform with a phone number. Reproduces phone-only mishearing. | 4 | A number to dial, a public address | Both |
| 4 | **Acceptance report.** Scorecard with intervals, fault classes, per-field tables, baseline comparison, and a signed, dated export (hash of inputs and results) | The document a client or a safety officer files. Nopaque sells exactly this for finance. | 3 | 1, 2; scorecard in flight | Both |
| 5 | **Ingest and capture.** `ingest` (list an agent's calls, deterministic failure rule) and `capture <call-id>` (audio, trace, word-timed transcript) | Loop steps 1–2: failures found without a person reading transcripts | 4 | Full tool results in the trace | Both |
| 6 | **Consent and provenance in the clip bank.** Manifest columns, batch refuses unconsented clips, `forget` with propagation, clips outside the repository by default | Makes real-clip testing lawful to sell. Nobody documents this. | 3 | — | Product (and protects the proof) |
| 7 | **Call-to-scenario with a proposed expectation, plus fault classes** (loop steps 4 and 6) and the fix brief (step 7) | A failed live call becomes a reviewed regression test in minutes | 5 | 5 | Both |
| 8 | **Join the two products.** A results API in the backend that stores caller runs per workspace; the dashboard testing pages read them; the text harness stays as the fast first rung | A buyer can log in and see results. Ends "two products in one repository". | 6 | 2, 4 | Product |
| 9 | **Second and third platforms proven on real accounts** (Retell, Vapi) and a generic "phone number plus webhook" mode with no adapter (grade from the rig's transcript and read-back only) | A first customer who is not on ElevenLabs | 5 | 3; adapters in flight | Product |
| 10 | **Line conditions on recorded clips.** Noise, level, band-limiting and packet loss applied to existing clips, as named conditions (pattern: `openai-cookbook` `shared/audio/effects.py`) | More coverage from the same consented recordings, without synthetic voices | 3 | — | Both (hearing faults) |
| 11 | **Scheduled runs and a CI gate.** A GitHub Action that runs a batch and fails on `compare` regressions; a cron entry for nightly runs | Table stakes for any buyer; cheap once `compare` exists | 2 | 1 | Both |
| 12 | **Healthcare evidence pack.** Export shaped as hazard-log evidence (hazard, control, test, result, date, versions) | The third remaining edge; a reason for a UK receptionist vendor to choose us | 4 | 4; a clinical safety officer's review of the format | Product |

### What not to build

- **A prompt-rewriting fixer.** Bluejay and Cekura ship one. It suits prompt-driven agents and is weak for code-decided flows. A fix brief is enough.
- **Synthetic personas, accents and voice libraries.** Every competitor has them. If a scenario has no recording, generate one clip with any speech service and label it synthetic in the manifest.
- **A judge-model metric suite, metric labs and human-review queues.** Keep the one text judge already in the backend for its own harness. Our value is the absence of a judge in the verdict.
- **Monitoring dashboards, alerts and trend charts for live traffic.** `ingest` exists to feed tests, not to become an observability product.
- **SIP and WebRTC transports, load testing, an MCP server, SSO.** None is near the edge. Revisit when a paying customer asks.
- **Mock tools.** Mocking removes the stored record, which is the thing we grade.

## What I could not check

- Whether any competitor does more than its public pages say; Hamming's docs are behind a login.
- Whether Cekura's clip features play the supplied audio (its changelog and FAQ disagree); the check on the bought seat settles it.
- PR #25's code line by line: I read its description and file list, not the diff.
- Anything live: no call was placed and no competitor account was used.
