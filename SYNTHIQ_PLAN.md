# Synthiq — the test layer

**Updated:** 27 August 2026
**Branch:** `claude/repo-robustness-analysis-6mru4e` → PR #8 against `voice-prod`
**Status:** four phases done, audio not started

This supersedes `VOICENOOB_QA_IMPLEMENTATION_PLAN.md` (December 2025) as the
working plan. That document is kept for history, but read it with a caveat: it
assessed the framework as *"85% production-ready, ~1.5 hours of P0 fixes
remain."* The application did not import. The evaluation model had been retired
so every call failed, the circuit breaker raised `TypeError` and had never once
guarded a call, and two Alembic heads meant migrations could not apply. None of
that was visible because nothing ran in CI.

That gap between believed and actual state is the whole reason this project
exists, and it is worth remembering when reading any status claim below —
including these.

---

## 1. What has been completed

Eighteen commits, in four phases.

### Phase 0 — make it real

| Commit | What |
| --- | --- |
| `da4c2d9` | CI pipeline: Postgres 17 + Redis 7 service containers, import guard, ruff, mypy, single-Alembic-head assertion, pytest, frontend gate |
| `c9b167f` | Replaced the retired evaluation model; real per-model cost table; removed the silent fallback that hid the failure |
| `e3bb2b0` | Circuit breaker fixed to `call_async`; registration rate limit repaired; two Alembic heads merged |
| `dd186f0` | Realtime client moved to the GA namespace with all ten event names; unused `pipecat` dropped |
| `829b427` | Event-loop lag gauge and the OpenTelemetry call-trace schema (`voice.call`, `voice.turn`, `voice.tool_call`) |
| `1b1ed4f` | Test suite moved onto real Postgres; 33 failures fixed |
| `90fcb15`, `13d0a1f` | MSW handler paths corrected; prettier applied |

The Realtime move is worth singling out. The two OpenAI SDK namespaces emit
**different event names** — `client.beta.realtime` sends `response.audio.delta`,
`client.realtime` sends `response.output_audio.delta`. Renaming the handlers
without moving the connect call would have silently killed every call. Both
moved together.

### Phase 1 — make the verdict mean something

| Commit | What |
| --- | --- |
| `423a3c4` | Deterministic metric core: registry, `MetricContext`, four-layer taxonomy |
| `98987e3` | Assertions decide the verdict, not the judge |
| `85754e1` | The agent's real tools execute during a simulated run |
| `a3699ec` | Rate-limiter and MSW ordering fixes — 13 tests failing for reasons unrelated to the code they were in |
| `e91669e` | Seed, isolate and provably roll back a run's database state |
| `5f08f0e` | Runs isolated by default; the rollback recorded on the run |

Two invariants carry most of the weight:

- **`value=None` means *not measurable*, and is strictly distinct from `0.0`,
  which means *measured and bad*.** Before this, a JSON parse failure was
  written as `score: 50, passed: False` — below the pass threshold — so every
  harness malfunction raised a failure alert about an agent that may have
  performed perfectly.
- **A broken run reports `ERROR`, never `FAILED`.** Validation gates run first;
  if the run is not trustworthy, nothing computed after it is reported as an
  agent result.

Isolation was also a bug fix, not only a feature. Once tool binding landed,
`run_scenario` had been executing real tools against the caller's real database
and leaving the rows behind — every test run quietly wrote into someone's CRM.

### Phase 2 — make it reachable

| Commit | What |
| --- | --- |
| `3272f5b` | The pytest-native API — a scenario is an argument, not a database row |
| `66f071a` | `POST /api/v1/testing/check` and the promptfoo provider in `integrations/promptfoo/` |

```python
result = await runner.check(
    agent=agent, user_id=user.id,
    says=["I'm Jane on 5551234567, book me for tomorrow"],
    invokes=["book_appointment"],
    leaves={"appointments": [{"status": "scheduled"}]},
)
assert result
```

Nothing is persisted. The judge is off by default — deterministic assertions
decide, so CI should not pay a model to narrate a conclusion the assertions
already reached. `RunResult` is falsey with a `__repr__` that *is* its
explanation, so a pytest failure prints the failing metric, the state diff, the
tools invoked, and what the agent said.

### Phase 3 and 4 — make it useful daily

| Commit | What |
| --- | --- |
| `7ddf8cf` | Mutations: A/B two configurations with Wilson intervals and an honest verdict |
| `1df0104` | An adaptive caller that reacts instead of reading from a script |

A comparison runs each variant `repeats` times (5 by default, not 1), because a
voice agent is stochastic and one run against one run measures variance and
reports it as a difference. **A winner is named only when the confidence
intervals do not overlap**; otherwise the comparison says it was inconclusive
and roughly how many more runs would separate them. 3/5 against 4/5 looks like
an improvement and is nothing of the sort.

---

## 2. Where we are

Verified on this branch, not estimated:

| | |
| --- | --- |
| Backend tests | **585 passing**, 0 failing |
| Frontend tests | **283 passing**, 0 failing |
| Type checking | `mypy --strict` clean across 101 files |
| Lint / format | ruff, prettier, eslint, tsc all clean |
| Migrations | Chain builds from scratch on Postgres; downgrades cleanly |
| Event-loop lag | 0.5 ms p95 idle, 748 ms max under a deliberately blocked loop |
| CI | Green on PR #8 |

### The honest gap

**No audio has ever passed through this harness, and no call has ever gone
through Telnyx.** Every result above comes from a scripted model and a local
Postgres.

What exists is a rigorous test harness for **text-driven agents with real tool
execution and provable rollback**. That is genuinely differentiated and testable
today. It is not yet a **voice** test harness. Do not demo it as one until a
real call has run.

The qualitative judge is also uncalibrated: nothing measures whether it agrees
with a human, so its scores are opinion rather than measurement.

---

## 3. The vision

Not another voice platform — **the layer that grades them.**

Four serious teams now ship voice-agent evaluation: Vapi, Pipecat, LiveKit,
promptfoo. Every one of them only tests agents built on itself. Coval, Hamming,
Cekura and Roark are provider-agnostic but are US-hosted SaaS, closed source,
with private deployment reserved for enterprise contracts.

The position nobody occupies is the harness that is provider-agnostic **and**
runs inside the customer's own infrastructure **and** can be read, audited,
self-hosted and forked.

The commercial shape follows from that:

1. **Open-source harness** — free forever, MIT or Apache-2.0, never BSL.
   Source-available would destroy the auditability being sold.
2. **Hosted platform** — EU-region cloud for teams who do not want to operate it.
3. **Enterprise** — SSO/SCIM, audit logs, RBAC, residency guarantees, SLA, and
   DORA/HIPAA compliance packs.
4. **Reseller and affiliate** on top, which only works because the harness is
   genuinely free and genuinely forkable.

This is the Langfuse model, and it is the one to copy precisely. Fixa built good
open-source voice tooling and stalled because its founders said the
monetisation model "didn't really make sense." That question gets answered
before more code is written, not after.

---

## 4. What it is looking like

Three research passes broke three claims this plan previously made. Recording
what did *not* survive matters more than restating what did.

| Claim | Verdict |
| --- | --- |
| "Nobody verifies backend state" | **False.** Coval ships an `API State` metric. It reaches state through an HTTP endpoint the customer builds and maintains; no database access appears anywhere in its public API, and no fixture or teardown primitive exists in any of its thirteen OpenAPI specs. |
| "The only open-source voice test harness" | **False.** Fixa (MIT, YC), LangWatch Scenario, LiveKit Agents evals, ServiceNow EVA and Voice Lab all exist. Fixa is effectively deprioritised, but the slot is occupied. |
| "EU law blocks US SaaS, so residency is the moat" | **Overstated.** The DPF is valid law, EUCS had its sovereignty requirements removed, the NHS permits approved US cloud, and US hyperscalers hold ~70% of the European cloud market against ~15% for EU providers. It is a sharp wedge into a real minority — SecNumCloud-scope French public bodies, German sovereignty programmes, DORA-classified bank functions, special-category health data — not a mass market. |

### What survives, and why it locks

Three pieces. Each alone is weak and copyable; together they are the argument.

1. **State verification with no integration to build.** Competitors assert state
   by calling an endpoint the customer wrote, authenticated and kept
   semantically in sync with the test. We snapshot the database and diff it.
   Their integration is a project; ours is a connection string.
2. **A real fixture lifecycle.** Isolated scope, seeded fixtures, deterministic
   assertions, guaranteed rollback, and an audit record proving nothing
   production was touched. No competitor documents teardown at all. Without
   provable rollback, "we test against your real database" is a sentence that
   ends procurement conversations rather than starting them.
3. **It runs inside the customer's walls by default** — and this is what makes
   the first one *legal*. No bank hands a US SaaS direct database access, which
   is precisely why Coval had to design around an HTTP endpoint. They are
   outside the wall and structurally always will be. Their architecture is a
   consequence of their business model; they cannot change one without the
   other.

### Where we are behind

- **Audio.** Coval tests accents, interruptions and background noise. Pipecat
  Evals runs synthesised caller audio through real STT with a virtual
  microphone. This is parity work now, not a moat.
- **Surface area.** Coval has scheduled runs, dashboards, multi-run reports,
  human review queues, four test input types (scenario, transcript, audio,
  script), and a forward-deployed-engineer agent. We have none of that.
- **Distribution.** Coval ships three surfaces — Agent Skills (MIT, open
  source), an MCP connector, and a Homebrew CLI. They open-sourced the
  *distribution layer* while keeping the product closed. That move is available
  to us in a stronger form, because our engine is the open part.

---

## 5. How the flywheel works

Four loops. The first two are already turning; the third and fourth are what
the remaining phases unlock.

### Loop 1 — adoption

```
pytest-native API + promptfoo provider
        ↓
a developer runs one test in under fifteen minutes
        ↓
it catches something a transcript judge missed
        ↓
they write more scenarios
        ↓
the harness becomes what their CI depends on
```

The entry cost has to stay near zero. Every open-source eval tool that inflected
— promptfoo, DeepEval, Ragas, Langfuse — did it with a familiar interface rather
than a novel dashboard, and permissive licensing. EfficientAI built our
architecture, MIT, and sits at 73 stars: **another standalone dashboard is not a
distribution strategy.**

### Loop 2 — production failures become tests

```
a real call goes wrong in production
        ↓
its trace is already in the OTel schema the harness reads
        ↓
one click turns it into a scenario with a fixture and an expected end state
        ↓
it runs on every pull request from then on
        ↓
that failure never ships twice
```

This is the loop that makes the harness stickier the longer it runs. It needs
the transcript and audio input types, which is phase 6 below.

### Loop 3 — the data moat

```
every run emits a trace in a provider-neutral schema
        ↓
traces across providers become comparable
        ↓
"Deepgram vs ElevenLabs on your traffic, with your metrics"
        ↓
a benchmark nobody else can produce, because nobody else
sits neutrally across all of them
```

Coval, Hamming and Cekura all see cross-customer data, but each is a vendor
selling a verdict. A harness the customer *runs themselves* can offer the
comparison without the conflict — and can offer it on their data, not a public
benchmark.

### Loop 4 — compliance compounds

```
red-team scenarios run over a real phone line
        ↓
the report maps to OWASP LLM Top 10 / NIST AI RMF / EU AI Act
        ↓
that report is the procurement artifact a regulated buyer needs
        ↓
they cannot get it from anyone else for a voice agent
        ↓
they adopt the harness to get the artifact
        ↓
which puts us back in loop 1, inside a regulated account
```

The EU AI Act obliges adversarial testing of high-risk systems. Yapper — sixteen
commits, MIT — proved you can dial a real number and run OWASP attacks against a
live voice agent. Nobody has productised it. promptfoo has the taxonomy and the
compliance mapping but is text-only; Pipecat has audio but no red team and only
inside Pipecat.

**The loops reinforce each other:** open source drives adoption, adoption
produces traces, traces produce the benchmark and the regression corpus, and
compliance converts the whole thing into contracts that fund it.

---

## 6. What remains

Ordered by value, not by ease.

| # | Work | Why | Size |
| --- | --- | --- | --- |
| 5 | **One real call, end to end** | Nothing here has touched live telephony. Until a real call runs through Telnyx and lands a row, the harness is unproven against the thing it exists to test. Blocked on credentials. | 1 day |
| 6 | **Audio in the loop** | Word error on intended-vs-transcribed, time to first audio byte, barge-in, DTMF, per-speaker tracks. Borrow Pipecat's virtual-microphone cadence trick and Future AGI's per-speaker recording. Also unlocks the transcript/audio test types loop 2 needs. | 1–2 weeks |
| 7 | **Red team over a real line** | The compliance artifact. Rides on the telephony driver phase 6 builds, then it is mostly scenario content plus the OWASP and EU AI Act mapping. | 4–5 days |
| 8 | **Judge calibration** | Human-vs-machine label agreement, then prompt refinement. Until this exists, judge scores are opinion. Needs human-labelled runs to be worth anything. | 3 days |
| 9 | **Scheduled runs and reports** | Continuous evaluation rather than only CI. Run templates on cron, regression detection against a baseline. | 4–5 days |
| 10 | **The Synthiq rename** | Repo, packages and API still say Voice Noob. Nearly free today; a migration once a design partner integrates against the provider. | 1 day |

---

## 7. Decisions still open

**Licence: MIT or Apache-2.0. Not BSL.** Every open-source eval tool that
inflected was permissively licensed, and source-available would destroy the
exact trust and auditability being sold.

**Monetisation, decided before more code.** Harness free and forkable forever;
sell hosted EU-region cloud plus enterprise features. Fixa's founders shipped
good open-source voice tooling and stalled precisely here.

**The OpenAI question, before procurement asks it.** promptfoo was acquired by
OpenAI in March 2026 — MIT licence and public repo retained. Selling
provider-neutrality to regulated buyers while depending on an OpenAI product
will come up in a security review. The answer is real (pin and fork), but put it
in the deck rather than wait to be asked.

**Engine choice, kept out of the test layer.** LiveKit Agents (Apache-2.0,
~120 plugins) and Pipecat (BSD-2, 60+ services) both solve provider optionality.
Pick one for the *hosted* product, but do not let it leak into the harness: the
metrics read a `MetricContext`, not a pipeline, and that is exactly why the same
code can grade a Vapi agent and a Pipecat agent.

**Avoid `rapidaai/voice-ai`.** GitHub reports GPL-2.0 with a condition requiring
visible Rapida branding and a paid licence to remove it. Copyleft plus mandatory
branding is structurally incompatible with a white-label reseller model, and it
is fixable only by not starting. Have a lawyer confirm before any of it reaches
the repo.

---

## 8. What is unverified

Stated plainly so nobody builds a pitch on it.

- Coval's fixture and teardown gap is inferred from what their public API
  *omits*. Two independent passes reached it, but absence of evidence is not
  proof. `curl -s https://api.coval.dev/v1/openapi | jq .` and look for
  workflow, state or teardown schemas before this appears in a sales
  conversation.
- Hamming being "state-blind" comes from Coval's own comparison page. Hamming
  disputes it. Both are marketing.
- Fixa's status — genuinely abandoned or merely quiet — rests on founder
  comments, not a commit history.
- Several claims about promptfoo, LiveKit, Pipecat and pi.dev internals come
  from search summaries rather than primary documentation, because those domains
  were unreachable from the environment this was researched in.
- Star counts and market-sizing figures are secondary sources; the voice-AI
  investment numbers originate in Coval's own fundraising PR.

---

## Appendix — the one-line version

Everyone else can tell you the agent said the right words.
We can tell you whether the appointment exists.
