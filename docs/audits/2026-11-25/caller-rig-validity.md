# Caller rig validity audit (25 Nov 2026)

**Question:** is a pass from this rig true, and is a fail true?

**Scope:** `caller/` on `entranoweb/voice-noob` main at `56702fa` (after #24). Read-only. No calls placed, no code changed. Open PR #25 was read only to avoid repeating work in flight; it was not audited.

**Not repeated here (in flight elsewhere):** full tool results in the ElevenLabs trace, `validate` prerequisite honesty, apostrophe encoding, scorecard, reset / read-back / chains (#25), Retell and Vapi adapters.

**Verdict:** the rig is sound as a reproduction tool and as a regression signal. It is not yet sound as the instrument that certifies "95%". Three things stand between the two: calls the rig itself spoils are counted as agent failures or silently dropped; a booking can pass with the wrong slot, type, phone or postcode; and the 58/60 gate does not demonstrate 95% even if every call is valid.

Severity key: **FP** = can produce a false pass. **FF** = can produce a false fail. **H** = hygiene.
Status key: **CONFIRMED** = read in code. **PLAUSIBLE** = follows from the code but depends on live behaviour not observed.

---

## 1. Gate arithmetic

Client test: 20 calls, suspended below 90%, so the agent must pass at least 18.

| True pass rate | Passes client test (≥18/20) | Passes our gate (≥58/60) |
|---|---|---|
| 90% | 68% | 5% |
| 93% | 84% | 20% |
| 95% | 92% | 42% |
| 96% | 96% | 57% |
| 97% | 98% | 73% |
| 98% | 99% | 88% |

- **What 58/60 proves.** The one-sided 95% lower bound on the true rate is 89.9% (exact binomial); the Wilson interval the rig prints is 88.6% to 99.1%. So 58/60 demonstrates "about 90%", not 95%.
- **What would prove 95%.** With zero failures allowed: 59 calls. With one failure: 93. With two: 124. This is the standard success-run plan, n = ln(1 − C) / ln(R) ([rule of three](https://en.wikipedia.org/wiki/Rule_of_three_(statistics))).
- **The gate is harsh on a good agent and still lets a weak one through.** A 96% agent fails the gate 43% of the time. A 93% agent passes it 20% of the time and then fails the client's 20 calls 16% of the time.
- **All of the above assumes 60 independent calls. They are not** (finding 4). If failures are per-scenario and deterministic, 60 calls carry about 20 calls' worth of information.

Recommendation: state the gate as a plan, not a count. Either 59 valid calls with zero agent failures, or 124 with at most two, each from a distinct scenario-and-clip combination, with voids re-run and reported.

---

## 2. Findings that can make a pass false

### F1. A booking passes without checking what was booked — FP, CONFIRMED

- `grading.py:254-265`: a tool expectation passes when any call whose name contains the fragment has the wanted `ok`. Nothing looks at arguments or results.
- `scenario.py:56-61`: `ToolExpectation` has only `name_contains`, `ok`, `called` on main.
- Scenarios 01 to 07 expect `v2_register_patient` and `v2_book_` and check only `name` and `date_of_birth`. Their personas carry `phone` and `postcode`, which are never checked.
- Consequences: scenario 07 ("any Wednesday") passes on a Thursday booking. Scenarios 05 to 07 (MRI) pass if a consultation is booked, because `v2_book_` matches either tool. A wrong phone number or postcode stored on registration passes.
- #25 adds `expect.tools[].arguments`. No scenario file uses it yet, so the false pass remains until the scenarios are rewritten.
- Outside practice: the OpenAI cookbook harness grades required tool calls with their arguments and the final application state, and warns that "a natural-sounding confirmation does not prove the correct backend actions were completed" ([voice agent evaluation](https://github.com/openai/openai-cookbook/blob/main/examples/audio/voice_agent_evaluation.md); `run_harness/graders.py` holds `expected_state` and `final_state` side by side). The G2/Bluejay method passes a call only when tool adherence and database state both hold, with no partial credit (§5.1, §5.5).

### F2. Agent failures can drop out of the pass rate — FP, CONFIRMED

Three separate paths remove a failed call from the denominator.

1. **Agent never answers.** `director.py:101` calls `leg.connect()` with no handler. `batch.py:265-267` and `batch.py:303` (`asyncio.gather` without `return_exceptions`) let that exception abort the batch. A refused connection, a concurrency limit, or on the phone leg a busy or unanswered line (`twilio_media.py:175-181`) is not recorded as a failed call. `batch.json` is not written.
2. **Agent dies early.** `batch.py:155-163`: a trace with no tool calls and no step path "looks unfinished", so the call becomes `ungraded`. An agent that greets and then fails before its first tool call, on a platform that records no step ids, lands here.
3. **Ungraded and not-run are excluded.** `report.py:67-68` and `report.py:102-103`: the rate is passed / (passed + failed). There is no rule that a gate needs a minimum number of graded calls per scenario, and no rule that an unexplained ungraded call counts against the agent.

Outside practice: G2/Bluejay §5.6 separates the cases explicitly. A call the platform refused before connection is void and re-run. A call that connected and then dropped audio, played an error, or lost a tool request is a **break**, graded as a fail and reported as a break rate. The rig has no such split.

### F3. Ground truth for "said" is another recogniser's reading of the clip — FP and FF, CONFIRMED

- `clipbank/build.py:89-91`: a clip's `said`, `intended` and `value` are all the batch transcript text. `build.py:94` leaves `checked_by_human` empty.
- Scenario facts are written from those values. If the batch recogniser misread the clip, the scenario's "truth" is wrong.
  - The live agent hears the clip correctly, stores the right value, and **fails**.
  - The live agent makes the same misreading (both recognisers come from one vendor), stores the wrong value, and **passes**.
- The earlier analysis already measured how often the two disagree: names 23 of 80, postcodes 16 of 53, dates of birth 8 of 68.
- Nothing enforces review. `clips.py:63-71` does not read the manifest's `review` column at all, `cli.py:122-145` (`validate`) checks only that files exist, and the director plays any clip. The 64 flagged clips are avoided by convention only.
- Outside practice: G2/Bluejay "certify" every task before use (§4.2) and audit a seeded 25% sample after each run, including "a seeded sample of passing calls for false passes" (§8.4). The cookbook says "approved human recordings are strongly preferred" and keeps reference transcripts evaluator-only.

### F4. Repeats are not independent, so the interval is too narrow — FP, CONFIRMED

- `report.py:103` pools every graded call into one Wilson interval (`stats.py:8-22`).
- Repeats of one scenario replay identical audio into the same agent. Hearing errors are close to deterministic per clip, so three repeats are nearer to one observation than three.
- One tester's voice, one recording channel, and scenarios cut from the same 81 calls the fixes were tuned on: the set measures regression on known cases, not the client's next 20 improvised calls.
- Outside practice: τ-bench reports pass^k, the share of tasks that pass all k trials (`sierra-research/tau2-bench`, `src/tau2/metrics/agent_metrics.py`, `pass_hat_k`). G2/Bluejay publish "consistency pass5, the share of tasks passing all five runs" beside the raw rate (§6.2). The cookbook asks for "task-completion rates and sample counts" from repeated runs and says "a single run is not a performance benchmark".

Recommendation: report three numbers: scenarios passing all repeats, scenarios passing any, and the raw rate. Compute the interval over scenarios, not calls.

### F5. A call that never finished can pass — FP, CONFIRMED

- `director.py:205-206`: reaching `max_seconds` ends the call as "caller hung up: time limit". `grading.py:292-294` then grades on tool checks alone.
- A call where the tools succeeded and the agent then looped, went silent, or told the caller something different from what it booked, passes. Nothing compares the agent's spoken read-back with the stored value, and nothing requires a normal ending.
- The tester marks such a call failed. The earlier analysis found three "successful" calls that stored a wrong date of birth after a read-back; the reverse case (right value stored, wrong value spoken) is invisible to the rig.

### F6. A mutation outside the declared level is not noticed — FP later, CONFIRMED

- `batch.py:231-240`: the guard compares the scenario's self-declared `mutates` with the batch allowance. It cannot see what the agent did.
- Scenario 17 (fee declined) is `none`. If the agent registers the patient anyway, the call is graded failed, but the patient row stays and no cleanup is triggered. Later "new patient" scenarios using that name meet an existing patient.
- `cli.py:269-275`: the single-scenario `run` command has no mutation guard, no name budget and no empty-trace retry. `synthiq-caller run scenarios/sophie/01-….json` books with no flag.

---

## 3. Findings that can make a fail false

### F7. When the rig cannot answer, the agent is failed — FF, CONFIRMED

- `director.py:164-170` matches the agent's line against fixed patterns. `director.py:198-201`: no match is logged as `no_answer_needed` and the caller stays silent.
- If the agent rewords a question ("Could I take your surname first?"), the rig is silent, the agent re-prompts and gives up, and `grading.py` fails the call on missing tools. The outcome is `failed`, indistinguishable from an agent fault.
- The target has 60 generated lines and improvised re-ask lines, so wording varies call to call. This will fire.
- `max_uses: 2` on almost every step (`scenarios/sophie/*`): a third ask of the same question gets silence where a person would answer again.
- Outside practice: G2/Bluejay §8.2 attributes every failure to one of seven causes; cause 2 "the caller departed from its script" and cause 3 "harness" are **void and re-run**, never charged to the platform. Their threats section also admits their matcher "sees only the agent's latest turn".

Recommendation: add an outcome `void: caller could not answer` when the last unanswered agent line ends in a question and the call then ended without the scenario's end pattern. Exclude it from the rate, report the count, and treat a void rate above a few percent as a rig defect.

### F8. Phone leg: every answer is about three seconds late — FF, CONFIRMED in code, not run

- On the phone leg the agent's words come from a transcriber that emits only final results (`transcribe.py`, `DeepgramTranscriber`), so `AgentText` arrives after the agent has finished the phrase (`twilio_media.py:233-234`).
- `director.py:131` then calls `tracker.mark(now)`, which clears "spoke since mark" (`turns.py:57-60`). The audio was fed before the mark, so `turns.py:92-97` falls through to "no audio started", which waits `audio_start_wait_s` = 2.5 s (`turns.py:41`).
- Result: transcriber delay plus a fixed 2.5 s before every answer. People answer in well under a second. The target has 54 no-answer exits; a three-second silence is exactly what trips them.
- The socket leg does not show this: across 32 answers in the saved runs the median excess over estimated speech time was about 1 s, and no answer started early.

### F9. Identity lock: the name budget allows three times what the target allows — FF, CONFIRMED against the known target

- `batch.py:109`: six calls per name per 900 s. The target locks a patient after two verification attempts in 900 s, across calls.
- Scenario 13 (wrong date, then right) spends both attempts in one call. Its next repeat inside 15 minutes fails for the lock, and is charged to the agent.
- The budget is in memory only, so two batches run back to back share nothing. It is keyed on the persona name, and "claire collins", "alice wonderland" and "mark marshall" are each shared by two scenarios.

### F10. Recogniser-dependent checks on the phone leg — FF and FP, CONFIRMED

- `grading.py:276-290`: `agent_says` and `agent_never_says` read the rig's own timeline, not the agent's trace. On the phone leg that text is a transcription of synthetic speech.
- Scenario 16 needs `999`. A transcriber that writes "nine nine nine" fails a correct call. A missed word passes a `never_says` check.

### F11. A mid-turn pause is taken as the end of the turn — FF and FP, PLAUSIBLE

- `turns.py:38`: 0.7 s of quiet ends the agent's turn. `director.py:179` joins everything pending and answers the first matching step.
- A read-back spoken in two parts with a gap ("I have the name as John Black." … "And the date of birth as …, is that right?") gets "Yes" after the first part. The rig confirms a date of birth it has not heard. Scenario 18 (date corrected at read-back) depends on not doing this.
- `turns.py:34`: the speech floor is a fixed RMS of 250. Any background or hold sound above it keeps the turn open until `max_turn_s` = 45 s.
- Outside practice: G2/Bluejay use an adaptive floor per channel, max(4 × 20th-percentile RMS, 0.01), over 50 ms windows (§6.4).

### F12. The caller never yields — H, CONFIRMED

- `director.py:176`: while a clip is queued the director does nothing. If the agent starts talking over the caller, the clip plays to the end. `clear_playback` exists on the phone leg and is never called.
- The pump sends digital zero between clips (`director.py:158-159`). A real line has a noise floor. End-of-turn models behave differently on perfect silence, so turn-taking faults seen with people may not reproduce.

---

## 4. Other confirmed points

| # | Point | Where | Sev |
|---|---|---|---|
| F13 | "Misheard" is over-counted. A postcode heard as words ("B forty-five eight HT") or a date heard as words with no month never normalises, so `heard_matches` is false though the hearing was right. Affects the said/heard table, not pass or fail. | `grading.py:106-135`, `145-148`, `172-177`; `report.py:93-94` | H |
| F14 | Only the last transcript fragment for a field is compared. | `grading.py:226-227` | H |
| F15 | "Stored" is the last value any tool received under a generic key, including bare `name`. Nested arguments are not searched. | `grading.py:209-217` | FF / H |
| F16 | A correct call with no tool calls on an engine with no step ids can never be graded. | `batch.py:155-163` | H |
| F17 | The caller hangs up the instant the end phrase arrives as text, before the agent has spoken it. | `director.py:182-185` | H |
| F18 | Lines that arrive during a step's `delay_s` are discarded. | `director.py:194-197` | H |
| F19 | On the phone leg each clip is resampled by a blocking `ffmpeg` call on the event loop, stalling audio for the length of the subprocess. | `clips.py:99`, `director.py:216` | H |
| F20 | Keypad digits are in-band tones. Whether the target platform turns in-band tones into keypad events is untested; if it does not, every keypad scenario fails for the rig's reason. | `twilio_media.py:188-190`, `dtmf.py:31-65` | FF, PLAUSIBLE |
| F21 | Scenarios run in file order, every time. No shuffle, so order effects are baked in. | `batch.py:298-302` | H |

The μ-law codec (`mulaw.py`) was checked against the reference algorithm by reading: sign, bias, segment and silence value are right.

---

## 5. Reproducibility

A run file holds the scenario name, the call reference, the timeline, the parsed trace and the grade (`director.py:45-52`, `batch.py:280-289`). It does not hold:

- the rig's commit;
- the agent's version, workflow revision or configuration;
- the scenario file's hash or the clip files' hashes;
- the turn settings, the leg type, or the wall-clock start;
- the raw conversation record (only the parsed trace is kept, so an adapter fix cannot be applied to old runs);
- any audio.

The evening lookup run could not say whether a changed behaviour was a fix or variance. This is why.

Outside practice: G2/Bluejay §2.4 freeze a versioned manifest before running and re-run everything on any change; §10 lists what is kept per call: manifest version, stack, task and run, stereo audio, platform record, reference transcript of both channels, every tool request, turn timings, and the grade with its cause and reviewer.

---

## 6. What a sceptical client could ask for that the rig cannot produce today

1. A recording of each test call, both sides.
2. The exact agent version each result was measured on.
3. Proof the calls went through the phone number they will dial. No call has gone over a carrier.
4. The scoring rule agreed in advance, and the rig's agreement rate with a human on the same calls. The 44 already-adjudicated calls from the current build are the obvious calibration set; nobody has replayed them and compared.
5. A demonstration that the rig fails a known-bad agent and passes a known-good one.
6. Held-out scenarios not used while fixing.
7. Response time per turn, re-ask count and call length. The timeline holds the raw data; nothing reports it. The tester fails calls for dead air and repetition.
8. The stored record read back from the system (in flight, #25).

---

## 7. Order of work

1. **Outcome classes** (F2, F7): `passed`, `failed`, `break` (agent or platform dropped the call: counts as fail), `void` (rig, scenario or fixture at fault: re-run, reported), `ungraded`. Catch connection failures per call.
2. **Scenario expectations** (F1, F5): slot, type, phone and postcode on every booking scenario; a required normal ending.
3. **Human-checked clips and facts** (F3): a reviewer confirms each clip's value; the loader refuses unreviewed or flagged clips unless told otherwise.
4. **Gate restated** (section 1, F4): zero-failure plan of 59 valid calls, or 124 with two; scenario-level reporting.
5. **Run manifest** (section 5): commit, agent version, hashes, settings, raw record, audio.
6. **Phone-leg timing** (F8) before the first carrier call is used as evidence.
7. **Budget matched to the target's lock** (F9), persisted across batches.
8. **Calibration** (section 6, item 4): replay the adjudicated calls and publish the agreement rate.

---

## 8. Not checked

- Nothing was run. Every timing claim on the phone leg is from reading code.
- The order of text and audio events on the live socket was inferred from 32 saved answers, not instrumented.
- Whether the target's booking tool can return a 200 with a refusal inside it (which `traces/elevenlabs.py:42-50` would count as success).
- Whether the target's tool arguments are flat (F15).
- PR #25 was not audited; some findings in section 2 and 3 may shift when it merges.
- The clip audio itself was not listened to.

## Sources

- OpenAI cookbook, voice agent evaluation: https://github.com/openai/openai-cookbook/blob/main/examples/audio/voice_agent_evaluation.md and `examples/audio/duplex_voice_agent_evaluation/run_harness/graders.py`
- G2 Voice AI Evaluation Methodology (28 Oct 2026), §§2.3, 2.4, 4.2, 5.1, 5.5, 5.6, 6.2, 6.4, 8.2 to 8.4, 10, 11: https://ai.g2.com/evaluations/voice/reports/G2_Voice_AI_Evaluation_Methodology.pdf
- LiveKit Agents testing guide: https://docs.livekit.io/agents/build/testing/ and `livekit/agents`, `examples/frontdesk/test_agent.py` (`is_function_call(name=…, arguments=…)`)
- τ-bench pass^k: `sierra-research/tau2-bench`, `src/tau2/metrics/agent_metrics.py`
- Rule of three and success-run sample size: https://en.wikipedia.org/wiki/Rule_of_three_(statistics)

All read 25 Nov 2026. No project, owner or client name was sent to any outside service.
