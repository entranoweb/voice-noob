# Synthiq Voice roadmap to 2027

**Written:** 24 November 2026
**Status:** proposed. Nothing here is built unless a section says so.

This document decides the engine, the flow-control model, the telephony legs, the
test pyramid and the product shape, and gives a dated plan. It follows this
repository's standing rule ([`HANDOFF.md`](HANDOFF.md), last lines): never claim a
capability the code does not have. Every external claim is marked **SOURCED** with
its page, or **INFERRED** when it is a judgement from sourced facts. Pages that
could not be read are listed in [Sources](#sources).

Where things stand on 24 November 2026, so the numbers below are read
correctly: the first agent this is being proven on handled 45% of its test
calls, which is a measured baseline; 95% is a target, not a result. The test
caller has placed 18 automated calls, all over the direct audio connection and
none over the phone network. A Twilio account with five UK numbers, one of them
unused, already exists.

A third mark was added the same evening: **RESEARCH**. It means "from research
read 24 Nov 2026, not tested hands-on". Those findings came back from a separate
research session after this document was first written and correct several
claims in it. Their source links are in that session's report and were not
re-fetched here, so treat each as a lead to verify, not as a fact.

The owner's direction, stated 24 November 2026:

1. The voice engine is self-hosted and runs on Azure in the UK, with Deepgram for
   speech recognition and a speech-synthesis provider that can be kept in the UK.
2. The automated test caller is ours, does not depend on any one engine, and is a
   product in its own right.
3. Pipecat may be adopted, LiveKit used if needed, Telnyx or jambonz for telephony.
4. Use it ourselves first, then productise and resell.

## Summary

| Question | Decision proposed | Main risk |
| --- | --- | --- |
| Engine | A cascaded pipeline: Deepgram speech recognition, a text model on Azure OpenAI in a regional deployment, region-pinned speech synthesis. **Framework not decided:** Pipecat is the leading candidate and LiveKit Agents runs beside it on the same clips | We have never run either; latency and turn-taking must be measured on real calls before anything is promised |
| Flow control | A server-side state machine (Pipecat Flows, programmatic) decides every step; the model only extracts values; spoken lines are fixed text wherever a line carries policy or data | Discipline: one prose-judged branch added "just this once" recreates the failure this replaces |
| Telephony, engine | SIP trunk into the engine, carrier chosen per customer; Twilio media streams while the trunk is being set up | UK number onboarding time on a new carrier |
| Telephony, test caller | Twilio now (the account and five UK numbers exist, one unused), SIP later so customers bring their own carrier | Repeated calls from one number being rate-limited or flagged. Twilio media streams do not run in the UK (RESEARCH), so this leg cannot carry a UK-only claim |
| Testing | A five-layer pyramid ending in production calls mined into tests | The audio layers need recordings and consent |
| Product | Nothing is sold as unique. Three things remain to prove (section 5): grading from the stored record by independent lookup, choosing the next real clip from what the agent just said, and evidence packaged for UK healthcare | No buyer has been asked. **Licence: the upstream README declares AGPL-3.0.** See [Licence](#licence-a-blocker-for-selling) |

## 1. Engine decision

**Conclusion: move from the speech-to-speech bridge to a cascaded pipeline.
Pipecat is the candidate framework, not a decision.** The reason for a cascade
is not quality. It is that the current engine cannot be pinned to the UK, and
cannot run a deterministic flow. Which framework carries the cascade is settled
by a side-by-side: the same flow on Pipecat and on LiveKit Agents, driven by the
same recorded clips from the test caller, compared on the numbers in "Main risk"
below. (RESEARCH: from research read 24 Nov 2026, not tested hands-on, which
found the case for Pipecat over LiveKit Agents weaker than this section first
stated.)

Verdict from that research on the two beliefs behind this section: "a realtime
speech-to-speech model on Azure cannot be pinned to the UK" **holds**; "a
cascade on Pipecat is the right base" is **half right**: cascade yes, Pipecat
unproven. Two findings against Pipecat, both RESEARCH (read 24 Nov 2026, not
tested hands-on):

- On LiveKit's own benchmark, Pipecat's Smart Turn cut callers off falsely
  35.2% of the time at 300 ms, against 9.9% for LiveKit's v1 turn detector. The
  benchmark is the competitor's, so it is a reason to measure, not a result.
- Pipecat Flows issue #6578: a node is left half set up when the caller talks
  over the greeting. The engine comparison must include a regression test for
  exactly that (section 6).

### What exists today

- Every call is one OpenAI Realtime speech-to-speech session bridged to a
  Telnyx or Twilio media stream at mu-law 8 kHz. There is no second path
  (`backend/app/services/gpt_realtime.py`, `backend/app/api/telephony_ws.py`;
  [`DECISIONS.md`](DECISIONS.md) §36). VERIFIED in code.
- `deepgram-sdk` and `elevenlabs` are declared and never imported. The pricing
  tiers that name Deepgram, ElevenLabs and Cerebras are not wired to anything
  ([`DECISIONS.md`](DECISIONS.md) §36). VERIFIED in code.
- No call has ever gone through a carrier ([`HANDOFF.md`](HANDOFF.md) §3).

### Options

| | A. Keep speech-to-speech (OpenAI Realtime, direct or via Azure) | B. Cascaded pipeline on Pipecat | C. LiveKit Agents |
| --- | --- | --- | --- |
| UK data residency | **No regional option.** Azure lists the realtime models as "available for global deployments" (SOURCED [1]). A global deployment can process anywhere | Each stage chosen for region: text model in a regional Azure deployment; Deepgram self-hosted or managed; speech synthesis region-pinned (see below) | Same choices as B for the model stages; adds a media server to host |
| Deterministic flow | Weak. One model hears, decides and speaks; a state machine can only be approximated with instructions and tools | **Yes.** Pipecat Flows: code decides transitions; needs a cascaded pipeline, "speech-to-speech models are not supported" (SOURCED [2]) | Partly. Agents, tasks and task groups; tool calls are model-driven and can trigger handoffs (SOURCED [3]) |
| Latency | Lowest: Azure quotes about 200 ms over WebSocket (SOURCED [1]) | Three hops. Deepgram quotes about 260 ms for end-of-turn detection (SOURCED [4]); the rest must be measured. INFERRED budget: 700 ms to 1.2 s to first audio | Similar to B. INFERRED |
| Telephony | Already bridged here (unproven on a carrier) | Serializers for Twilio, Telnyx, Plivo, Exotel, Vonage and a native SIP transport with DTMF (SOURCED [5], [6]) | SIP via a third-party trunk; separate SIP and egress services to run (SOURCED [7]) |
| Recording, DTMF, turn detection | Built by hand here; no audio capture today | Shipped: per-speaker recording, DTMF frames, voice-activity and smart-turn strategies (SOURCED [5], [8]) | Shipped: egress recording, DTMF on the SIP participant (SOURCED [7]) |
| Operational burden | Lowest | One Python service per call plus the model endpoints | Highest: media server, SIP service, egress, agents |
| Migration from today | None | Replace `gpt_realtime.py` and the two bridges; keep `AudioTurnRecorder`, the trace emitter, the tools registry and the database | Larger: the bridge, transport and deployment all change |
| Licence | n/a | BSD-2 (as recorded in `SYNTHIQ_PLAN.md` §7; not re-fetched) | Apache-2.0 (same caveat) |

### Why B is the first candidate

1. **Residency.** A UK healthcare buyer will ask where audio is processed. Option
   A cannot answer "in the UK" on Azure today (SOURCED [1]).
2. **Determinism.** The client agent this work started from failed mostly because
   a model was judging 939 prose conditions. The fix is a state machine with the
   model extracting values. Only B expresses that directly (SOURCED [2]).
3. **It already ships what we would otherwise build**: per-speaker recording,
   DTMF, turn detection, five carrier serializers and SIP (SOURCED [5], [6], [8]).
4. **Deepgram credits are usable at once.** Flux takes mu-law at 8,000 Hz and
   emits `EndOfTurn`, `EagerEndOfTurn` and `TurnResumed` (SOURCED [4]).

### The stages, and what is not yet known

| Stage | First choice | UK position | Not verified |
| --- | --- | --- | --- |
| Speech recognition | Deepgram Flux (`flux-general-en`) for turn-taking; Nova-3 as the independent referee transcript | Managed regions are the United States, the EU, Australia and India: no UK (RESEARCH). Self-hosting on Docker or Kubernetes needs an Enterprise plan (SOURCED [4], [9]); self-hosted Flux and Aura-2 need an Enterprise contract (RESEARCH) | Accuracy: one practitioner report has Nova-3 getting UK postcodes right 63.7% of the time on 248 real UK phone calls (RESEARCH: single unreplicated source, read 24 Nov 2026, not tested hands-on). So no recogniser is trusted with a postcode: read-back and keypad entry are designed into every flow that takes one |
| Text model | Azure OpenAI, regional (not global) deployment | Regional "Standard" deployments exist for text models (SOURCED [10]). In UK South, Standard regional, gpt-4.1-mini and gpt-4o are ticked (RESEARCH: read 24 Nov 2026, not tested hands-on) | Whether each is actually deployable in our subscription, and its retirement date. Confirm both before committing |
| Speech synthesis | Decide by listening test | ElevenLabs residency is EU, India or Singapore, Enterprise only, **no managed UK region**, and "processing may occur elsewhere" unless zero-retention mode and the API are used (SOURCED [11]). A private deployment in the customer's own AWS or GCP exists by enterprise quote; whether it can sit in London is unverified (RESEARCH). Azure Speech in UK South does speech recognition and neural speech synthesis in region; its HD voices are not available there (RESEARCH) | Deepgram Aura self-hosted quality; Cartesia regional hosting; ElevenLabs private deployment price and region. None tested |
| Turn detection | Flux end-of-turn, with a silence floor | n/a | Behaviour on callers who pause mid-answer |

**Main risk.** This pipeline has never run here. The honest sequence is: build it
beside the current bridge, place real calls through both, and compare first-audio
time, barge-in handling and word error rate using the metrics this repository
already has. Do not delete the speech-to-speech path until those numbers exist.

**Cost.** Modelled, not invoiced (RESEARCH: read 24 Nov 2026, not tested
hands-on). Per minute at 10,000 minutes a month, carrier and recording included,
in the research file's figures (US dollars, as its other prices are):

| Stack | Per minute |
| --- | --- |
| Pipecat with managed Deepgram | 0.077 |
| Vapi | 0.113 |
| ElevenLabs Agents | 0.124 |
| Retell | 0.153 |
| UK-contained stack, with an assumed $1,200 a month speech pool | 0.185 |

Read plainly: **the sovereign stack costs more than every hosted option until
volume is high.** The cheap line is the one that is not UK-contained. Quote no
sovereign price to anyone until there are invoices behind it.

## 2. Flow control

**Conclusion: the server decides the next step. The model never does.**

The root-cause analysis of the client agent found 939 of 1,292 exits judged by a
model against prose, two exits firing on one caller turn, and "ask again" lines
improvised by the model that asked a different question. Three rules follow.

1. **Transitions are code.** In Pipecat Flows, "functions do their work and
   return the next node" (SOURCED [2]). Use the programmatic form, not the
   declarative one, so every transition is a testable function.
2. **The model extracts, it does not route.** Each node gives the model one job:
   pull a name, a date, a yes or no, a slot choice out of the caller's words, as
   a function call with a strict schema. A plain server function then validates
   the value and picks the next node.
3. **Policy and data are spoken as fixed text.** Fees, read-backs, emergency
   wording, "nothing is free" and every "I did not catch that, <same question>"
   are literal strings or templates filled from tool results. Whether Flows'
   pre- and post-actions can speak fixed text was **not confirmed** on the page
   read (SOURCED [2]); if they cannot, push the text straight to the speech stage
   from the handler.

| Need | Pipecat Flows | LiveKit workflows | Hand-rolled |
| --- | --- | --- | --- |
| Code decides the transition | Yes (SOURCED [2]) | Tool calls are model-driven; tools can trigger handoffs (SOURCED [3]) | Yes |
| One job per step, small tool set | Yes, per node (SOURCED [2]) | Yes, per agent or task (SOURCED [3]) | Yes |
| Ordered steps with "go back" | Write it | Task groups let users revisit earlier steps (SOURCED [3]) | Write it |
| Visual editor | Yes, flows.pipecat.ai (SOURCED [2]) | Not seen | No |
| Works with speech-to-speech | No (SOURCED [2]) | Not stated | n/a |

Two engine-level rules that no framework gives for free, both learned from real
calls: an emergency check runs on every caller turn before routing, as code over
a word list, not as a judged condition on every node; and a caller turn is
consumed by exactly one step, so an answer that arrives late is re-asked, not
handed to the next question.

## 3. Telephony

**Conclusion: SIP for both legs in the product; Twilio media streams to get
moving.** The engine and the test caller are separate decisions.

| | Twilio | Telnyx | jambonz | Bring-your-own SIP trunk |
| --- | --- | --- | --- | --- |
| UK number onboarding | Local and national numbers need a UK address; mobile numbers do not; approval time not stated (SOURCED [12]) | About 72 hours after documents (SOURCED [13]) | Uses whichever carrier is attached | Carrier-dependent |
| Media to our service | Media streams, mu-law 8 kHz (in code today) | TeXML or call-control streams (in code today, with the `bidirectionalMode` trap in [`DECISIONS.md`](DECISIONS.md) §22) | `listen` verb (SOURCED [14]) | Pipecat native SIP transport (SOURCED [6]) |
| DTMF, per-channel recording | Supported; details not re-read | `send_dtmf`, `channels: dual` recording (SOURCED [15]) | `dtmf`, `play` verbs (SOURCED [14]) | DTMF both ways in Pipecat SIP (SOURCED [6]) |
| Operating cost | A vendor account | A vendor account | A telephony platform to run | A trunk and our own SIP endpoint |

- **Engine:** terminate SIP in the engine so a customer can keep their carrier
  and their numbers, and so media can be anchored where the engine runs. Until
  the trunk exists, Twilio media streams are the shortest path because the
  account and UK numbers already exist.
- **Test caller:** Twilio now; SIP as soon as a second customer asks. The caller
  must be able to dial any number on any carrier, which SIP gives and a single
  vendor's stream API does not.
- **jambonz:** not now. It is worth it only if we need to operate carrier
  routing ourselves. INFERRED.
- Media anchoring in the UK or EU was **not verified** for any carrier.
- **Twilio media streams run in the United States, Ireland and Australia**
  (RESEARCH: from research read 24 Nov 2026, not tested hands-on). There is no
  UK option, so call audio on the Twilio phone leg leaves the UK. That leg is
  for reach and speed; a UK-only claim needs the SIP leg anchored in the UK,
  and must not be made for the test caller until that exists.

## 4. Testing

**Conclusion: grade from the system's own tool results and stored data, on real
recorded voices, across chains of calls.** An earlier version of this line said
every existing product stops there. That was wrong; see "What the market does
and does not do".

### The pyramid

| Layer | What it proves | State on 24 Nov 2026 |
| --- | --- | --- |
| 1. Route and contract tests | Each tool route alone; "every slot the offer returns can be booked" against a seeded database | Exists for this repository's own tools; the contract pattern is new |
| 2. Text simulation | Flow logic with a persona caller, real tools, provable rollback | Built and tested (`backend/app/services/qa/`) |
| 3. Socket audio replay | A real caller's recorded answers played into the agent's audio socket; the live speech model, turn-taking and flow all exercised with no phone line | **Works**, in `caller/`: 18 automated calls placed against a test agent by 24 Nov 2026, each driven by recorded clips with nobody on the line |
| 4. Phone-leg replay | The same clips over a real carrier, so codec, jitter, DTMF and transfers are in the loop | Built and tested against a stand-in for the carrier; **no call has gone over the phone network**. The Twilio account and UK numbers exist |
| 5. Production mining | A failed live call becomes a test | Not built |

### The loop (layer 5)

1. A production call fails, judged from tool results, not a transcript opinion.
2. Its caller audio is cut into clips on word timestamps; overlapping speech is
   dropped; a person listens to each clip once.
3. The clips and the expected outcome become a scenario.
4. The fix is made.
5. The whole scenario set is re-run several times.
6. The change is promoted only if the pass rate holds, with Wilson intervals, and
   `Comparison.winner()` returning `None` when intervals overlap
   (`backend/app/services/qa/mutations.py`; [`DECISIONS.md`](DECISIONS.md) §12).

### What the market does and does not do

From [`COMPETITIVE_LANDSCAPE.md`](COMPETITIVE_LANDSCAPE.md) and the vendors' own
pages read on 24 November 2026: Hamming, Coval and Cekura all place real calls
with synthetic personas, accents, noise and interruptions, check tool calls, and
offer schedules and release gates (SOURCED [16], [17], [18]).

This section first claimed that nobody replays real audio, resets the system
under test, or tests state carried between calls. Each was inferred from absence
on the few pages read. The verdicts below are RESEARCH: from research read
24 Nov 2026, not tested hands-on.

| Belief this document held | Verdict | Why |
| --- | --- | --- |
| Nobody replays real recorded audio as the caller | **False** | Cekura (changelog, August and September 2026), Hamming, Bespoken and Sipfront all do or say they do |
| Nobody resets the system under test between calls | **Mostly false** | Cekura restores its mock tools after a test; Bluejay and G2 seed a fresh database per call. Still undocumented anywhere: a portable cleanup hook that runs against a customer's real back-end |
| Nobody tests state carried from one call to the next | **False** | Bluejay ships multi-call "Customer Journeys" |
| Azure realtime speech-to-speech cannot be pinned to the UK | **Holds** | Section 1 |
| ElevenLabs has no UK region | **Holds, with a caveat** | No managed UK region; a private deployment in the customer's AWS or GCP by enterprise quote, London unverified |
| A cascade on Pipecat is the right engine base | **Half right** | Cascade yes; Pipecat unproven (section 1) |

Who is in the market, with public prices (all RESEARCH, same date, same caveat):

| Product | What it is | Price |
| --- | --- | --- |
| Cekura | Simulated and replayed calls, monitoring. Already ships silence, keypad digits, interruption and speaking-speed behaviours | $0.25 per test minute, $0.05 per monitored call, $500 a month plan |
| Coval | Simulation and evaluation. $28M Series A, June 2026 | $100 a month for 100 simulation minutes, $500 for 1,000; overage $0.40 and $0.25 a minute |
| Hamming | Simulation, replay, release gates | Unpublished |
| Roark | Replay and metrics | $0.15 a minute pay-as-you-go plus $0.04 per metric per minute; enterprise from $4,000 a month |
| Bespoken | Supplied audio against IVRs and assistants | $2,000 a month for 5,000 interactions |
| Bluejay | Multi-call journeys, fresh database per call | Not recorded |
| Sipfront, Cyara | Telephony and contact-centre testing with supplied audio | Not recorded |
| Nopaque / TotalPath | **UK.** Real phone calls against IVRs and AI agents; signed compliance reports for EU AI Act Article 50, FCA PRIN 2A and Ofcom; API, SDKs, CI gate, MCP server. Grades whether the goal was reached, not the stored record. Aims at UK finance. Synthetic data only | $0.40 a minute |

Evalion is not a direct competitor and is dropped from this list. Free and
open-source testers (voicetest, Apache-2.0, and others) crowd the low end, so
there is no room to sell a basic simulated caller.

What remains for us is in section 5. It is narrower than this section first
said, and none of it is proven.

One finding from the prototype worth keeping: the same recorded opener, played
five times, was transcribed three different ways by the agent's live speech
model. A synthetic persona cannot expose that; a recorded clip can, and repeat
runs measure it.

### Staying ahead: five capabilities, in order

None of these is claimed as exclusive (see the tables above). Item 4 in
particular is already shipped by Cekura.

1. **Clip replay with said, heard, stored.** For every name, date, postcode and
   number: what the caller said, what the agent's speech model heard, what the
   tool received, what was stored.
2. **Reset hooks for the system under test.** A customer-supplied seed and
   restore step around each call, with the restore verified. This repository
   already does this for its own database (`fixtures.py`; [`DECISIONS.md`](DECISIONS.md) §5, §6).
3. **Call chains.** Register in call one, return in call two, fail identity and
   be locked in call three. State across calls is where real agents break.
4. **Misbehaviour on demand.** Talk over the agent, answer late, stay silent,
   key digits. Scripted, repeatable, and impossible in text.
5. **Trace adapters per engine.** One reader per platform that turns its call
   record into our `MetricContext`. The metrics never see a pipeline, which is
   already this repository's most valuable property ([`DECISIONS.md`](DECISIONS.md) §36).

Table stakes we still lack and must add alongside: parallel calls, schedules,
release gating from a batch run, and accent or noise personas for coverage beyond
the recorded voices.

## 5. Product

**Conclusion: nothing here is sold as "only we". Three things remain, all
unproven, and the first is the one to prove on our own agent. Do not sell
anything until the licence question is settled.**

- **Withdrawn.** "Only we replay real audio", "only we reset between calls",
  "only we chain calls" and "only we are in the UK". Others do each (section 4;
  Nopaque / TotalPath is a UK company).
- **What remains, all unproven** (RESEARCH for the market side; none of it
  tested with a buyer):
  1. **Grading from the stored record by independent lookup.** After the call,
     read the booking or patient record from the customer's system ourselves
     and report said, heard, stored per field. Today the caller grades from the
     agent's tool results; the independent lookup is not built.
  2. **Choosing the next real clip from what the agent just said.** Built, over
     the direct connection.
  3. **UK healthcare evidence packaging.** An export shaped as a DCB0129 /
     DCB0160 hazard-log entry. Not built.
- **Working pitch.** "Evidence your AI receptionist saved the right booking, in
  the format your clinical safety officer files."
- **Who buys.** Teams running a voice agent on any platform who have recordings
  and a backend they can seed. Regulated buyers who need evidence that a change
  did not make things worse.
- **Packaging.** The harness and caller as source; a hosted service in a UK or EU
  region for teams that will not run it. `SYNTHIQ_PLAN.md` §7 already proposes
  this shape.
- **Price reference points.** The table in section 4.
- **Pricing hypothesis for a first pilot.** No buyer has accepted it. £750
  one-off for one workflow with up to 400 test minutes; then £300 per named
  agent per month including 500 minutes; then £0.35 a minute.
- **Clip bank rules before any outside sale.** The first external sale uses
  consenting volunteers reading fictional details, not real callers. No voice
  cloning and no speaker embeddings. Every clip carries its provenance and
  lawful basis. Consent is tracked; redaction exists; deletion propagates to
  every copy and every scenario that uses the clip; a retention period is set;
  storage is in the UK.
- **Compliance.** This repository holds no certification and none should be
  claimed. Selling into UK healthcare will need, at least, a data-processing
  agreement, a statement of where audio and transcripts are processed and
  stored, a retention and deletion mechanism for recordings and clips (clips are
  voice data of identifiable people), and consent handling for any recorded
  voice used as test audio. The buyer's clinical-safety process (DCB0129 and
  DCB0160) will ask for a hazard log entry for the agent under test; the test
  tool supplies evidence to it and is not itself the safety case. INFERRED; take
  advice before quoting any of this to a buyer.

### Licence: a blocker for selling

- The upstream repository has **no licence file**; GitHub reports no licence for
  it or for this fork. Its README, and ours, end with "License: AGPL-3.0"
  (VERIFIED: `README.md` in both; GitHub API `license: null`, 24 Nov 2026).
- If that declaration stands, this repository is a derivative of AGPL-3.0 code.
  AGPL allows commercial use and hosting, but anyone who interacts with a
  modified version over a network must be offered its source, and derived work
  must stay under AGPL. It cannot be relicensed to MIT or Apache-2.0 by us.
- That contradicts `SYNTHIQ_PLAN.md` §7 ("Licence: MIT or Apache-2.0").
- A README line with no licence text is also ambiguous; with no licence at all,
  the default is that no rights are granted.
- This fork is currently **public** on GitHub (VERIFIED, GitHub API).

Options, for a lawyer to confirm:

| Option | What it means |
| --- | --- |
| Comply with AGPL-3.0 | Add the licence file, keep the fork open, sell hosting and support. Works for an open-core model; rules out closed features built on upstream code |
| Ask the upstream author for a commercial licence or a relicence | Cleanest if granted; get it in writing |
| Build the new work separately | The caller and trace adapters are new code with no upstream lineage. Kept in a separate repository that does not import this one, they can carry any licence. The engine, if rebuilt on Pipecat, could follow the same route |

Recommendation: the third option for the test product, decided before the
`caller/` package grows any dependency on `backend/app`.

## 6. Plan: 30, 60 and 90 days from 24 November 2026

Owners are blank on purpose.

| By | Deliverable | Owner |
| --- | --- | --- |
| 24 Dec 2026 | Licence decision taken with legal advice; `caller/` location follows from it | |
| | Socket audio replay in `caller/` with scenarios, said/heard/stored report and outcome from tool results; first trace adapter | |
| | First real call on the phone leg over Twilio (the leg is built; the account and numbers exist), recorded per side | |
| | Batch runner: N scenarios by k repeats, Wilson intervals, one command | |
| | Dependency and security hygiene closed: websocket token, signature check fails closed, call-back host from configuration | |
| 23 Jan 2027 | Pipecat pipeline beside the current bridge: Deepgram Flux, regional Azure text model, one speech-synthesis option; first real call through it | |
| | The same flow on LiveKit Agents, driven by the same clips, for the framework side-by-side. Includes a regression test for the caller talking over the greeting (Pipecat Flows issue #6578) and a false cut-off count for each turn detector | |
| | Side-by-side numbers on real calls for the current bridge and both candidate frameworks: first-audio time, barge-in, word error rate | |
| | One flow rebuilt as a programmatic state machine with fixed lines; emergency pre-check per turn; read-back and keypad entry for postcodes and other digits | |
| | Independent lookup of the stored record after each call; cleanup hook against a real back-end; call chains | |
| 22 Feb 2027 | Framework chosen (Pipecat or LiveKit Agents) on the measured numbers; speech-to-speech path removed or kept as an option | |
| | SIP leg for engine and caller | |
| | Production mining: failed call to scenario, under the clip bank rules in section 5 | |
| | Second trace adapter; schedules and release gating | |
| | Hosted deployment in a UK region, with a written statement of where data is processed; hazard-log export for one agent | |

## 7. Decisions needed from the owner

| Decision | Recommendation |
| --- | --- |
| Licence route | Take advice now. Keep the test product in separate, clean code so it is not bound by the upstream declaration |
| Whether this fork stays public | Decide deliberately; it is public today |
| Engine | Cascaded pipeline, built beside the current bridge. Pipecat first as the candidate, LiveKit Agents beside it on the same clips; choose on measurement before the old path is removed |
| Speech synthesis vendor | Choose by listening test among options that can be pinned to the UK. ElevenLabs has no managed UK region; ask for a quote on its private deployment in our own cloud before ruling it out (RESEARCH) |
| Deepgram: managed or self-hosted | Managed to start, which is not UK (regions are US, EU, Australia, India). UK-only processing needs the Enterprise contract or Azure Speech in UK South; open that conversation early if it is a sales requirement |
| The unwired pricing tiers | Delete them until the cascaded engine makes them true ([`DECISIONS.md`](DECISIONS.md) §36) |
| First paying customer profile | A UK healthcare team with an agent in production and a clinical safety officer who has to file evidence. Test the pilot price in section 5 on them |

## Sources

Read on 24 November 2026 unless noted.

1. Azure OpenAI Realtime API: <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/realtime-audio>
2. Pipecat Flows: <https://docs.pipecat.ai/guides/features/pipecat-flows>
3. LiveKit Agents workflows: <https://docs.livekit.io/agents/build/workflows/>
4. Deepgram Flux quickstart: <https://developers.deepgram.com/docs/flux/quickstart>
5. Pipecat serializers: <https://docs.pipecat.ai/server/services/serializers/introduction>
6. Pipecat v1.17.0 release notes: <https://github.com/pipecat-ai/pipecat/releases/tag/v1.17.0>
7. LiveKit outbound calls and self-hosting: <https://docs.livekit.io/sip/outbound-calls/>, <https://docs.livekit.io/transport/self-hosting/>
8. Pipecat audio recording: <https://docs.pipecat.ai/api-reference/server/utilities/audio/audio-buffer-processor>
9. Deepgram self-hosted: <https://developers.deepgram.com/docs/self-hosted-introduction>
10. Azure Foundry models: <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/models>
11. ElevenLabs data residency: <https://elevenlabs.io/docs/overview/administration/data-residency>
12. Twilio UK regulatory guidelines: <https://www.twilio.com/en-us/guidelines/gb/regulatory>
13. Telnyx UK number requirements: <https://support.telnyx.com/en/articles/1311457-united-kingdom-uk-did-requirements>
14. jambonz verbs: <https://docs.jambonz.org/verbs/verbs/overview>
15. Telnyx recording: <https://developers.telnyx.com/api-reference/call-commands/recording-start>
16. Hamming: <https://hamming.ai>, <https://hamming.ai/pricing>
17. Cekura: <https://cekura.ai/pricing>, <https://docs.cekura.ai/documentation/integrations/elevenlabs/testing.md>
18. Coval: <https://coval.ai/pricing>, <https://docs.coval.ai/concepts/agents/connections/inbound-voice>
19. Hamming on fixtures: <https://hamming.ai/resources/voice-agent-sandbox-testing-tool-calls-side-effects>

Sources 5 to 8 and 13 to 19 were read earlier the same day in the session that
produced this roadmap, not re-fetched while writing it.

**Not fetched, so nothing above relies on them:** Azure's region-by-region model
table (which text models have a regional deployment in UK South); Azure Speech
voices and regions; Cartesia; Deepgram Aura self-hosting; Deepgram regional
endpoints; any vendor's per-minute price for the model stages; carrier media
anchoring; the Pipecat and LiveKit licence files themselves.
