# Coval platform recon — starting prompt

Paste this into an agent that has your Coval account access. Install their surfaces first:

```bash
npx skills add coval-ai/coval-external-skills
brew install coval-ai/tap/coval
coval login
```

---

## THE PROMPT

You are doing competitive product research on **Coval** (coval.ai), a voice-AI agent
evaluation platform. I have a paid account and legitimate access. Use the Coval CLI,
the MCP connector, and the installed Coval skills to explore the live product.

Ground rules, and hold to them:
- **Quote, don't paraphrase.** When you find a field name, enum value, metric type,
  status value or error string, give it to me verbatim in a code span. Paraphrase is
  useless for this task.
- **Separate evidence from inference.** Prefix every claim with `[OBSERVED]` (I ran a
  command and saw this), `[DOC]` (their documentation says it), or `[INFERRED]` (I am
  reasoning from absence or from an adjacent fact). Never let an inference read as a fact.
- **Absence is a finding, but a weak one.** If you cannot find a feature, say
  "not found in the surfaces I checked, which were X, Y, Z" — never "they don't have it."
- **Report on Coval only. Never describe what our product does.** You do not have access
  to our codebase and you will get it wrong. A previous pass produced a comparison table
  listing `setup_fixture` and `teardown_fixture` hooks as capabilities we ship — we do
  not, they are unbuilt. Fabricated columns like that end up in pitch decks and become
  false claims to customers. If you want to produce a comparison, output the Coval column
  and leave ours blank for me to fill in.
- Read-only. Do not delete, modify or overwrite anything in the account. Creating
  throwaway test resources is fine; name them `recon-<date>-*` so they are identifiable.
- Normal account usage only. Don't scrape at volume, don't hammer their API, don't
  reproduce their proprietary source. We are studying a product we pay for.

Work through these six phases in order and report as you go.

### Phase 1 — Inventory the object model

Confirm or correct this hierarchy (I derived it from their public `coval-resources` skill):

```
Agent (22-char ID)
└── Mutation (26-char ID)
Test Set (8-char ID)
└── Test Case (22-char ID)
Persona (22-char ID)
Metric (22-char ID)
Run (22-char ID)
└── Simulation (22-char ID)
    └── Metric Output (26-char ID)
Run Template (22-char ID)
└── Scheduled Run (22-char ID)
```

For each resource: list every field, every enum and its allowed values, which fields are
required, and what the create/read/update/delete endpoints are. Hit
`https://api.coval.dev/v1/openapi` first and dump the schema names — that is the fastest
route to all of this. Then spot-check against the CLI (`coval --help` recursively) and
the MCP tool list.

### Phase 2 — The metric system

This is the most important phase for us. I need their metric taxonomy in full detail.

1. List **every built-in metric**, its exact name, what it measures, its output type, and
   whether it is universal / voice-only / chat-only. I believe the list includes Latency,
   Turn Count, Audio Duration, Transcript Sentiment Analysis, Speech Tempo, Time To First
   Audio, Interruption Rate, Background Noise, Words Per Message — confirm and complete it.
2. **Custom metric types**: I know of `llm-binary`, `audio-binary`, `pause-detection`
   (default threshold 3.0s) and `API State`. Find every other type. For each, give the
   complete configuration schema.
3. **Metric output types** are documented as FLOAT, STRING, SET, BOOLEAN. What is `SET`
   for — multi-label classification? Show a real example of a SET-valued output.
4. **The question I care most about**: when a metric cannot be computed — the API endpoint
   times out, the audio is missing, the run crashed mid-way — what does Coval store? Is
   there a state distinct from "failed"? Look for enum values like `ERROR`, `SKIPPED`,
   `NOT_APPLICABLE`, `NOT_FOUND`, `UNKNOWN` on Metric Output and on Simulation. Try to
   trigger it: point an `API State` metric at an endpoint that returns 500 or times out,
   and show me exactly what lands in the result.
   *Why: if a broken harness reports as a failing agent, the metric is untrustworthy. I
   want to know whether they made this distinction or not.*
5. Is there any metric whose value depends on the **state of the customer's system** rather
   than on the conversation? Other than `API State`, I expect none — confirm.

### Phase 3 — State, fixtures, and teardown  ← the decisive one

**Start from what a prior API inspection reported, and try to break it.** That pass claimed
the public `MetricType` enum contains exactly twelve values — `METRIC_LLM_BINARY`,
`METRIC_CATEGORICAL`, `METRIC_NUMERICAL_LLM_JUDGE`, `METRIC_AUDIO_LLM_BINARY`,
`METRIC_AUDIO_LLM_CATEGORICAL`, `METRIC_AUDIO_LLM_NUMERICAL`, `METRIC_TOOLCALL`,
`METRIC_METADATA_FIELD`, `METRIC_TRANSCRIPT_REGEX`, `METRIC_PAUSE_ANALYSIS`,
`METRIC_SQL_FLOAT`, `METRIC_COMPOSITE_EVALUATION` — with **no** `METRIC_API_STATE`, and no
fixture, setup, teardown or rollback primitive anywhere in the specs.

First: **independently re-confirm the enum**, verbatim, from the live spec. Then treat the
absence as a hypothesis to falsify, not a conclusion:

- Does `API State` appear anywhere in the **web UI's** metric creation flow, even if it is
  missing from the public spec? Walk the create-metric wizard and screenshot every option.
- Watch the **browser network tab** while creating a metric. Does the UI call a different
  host or API version than `api.coval.dev/v1`? A gated or internal API would show here.
- Search their docs site and skills repo for `API State`, `MATCH`, `DIFF`, `NOT_FOUND`.
  If documented, quote the page and note whether it is marketing copy or an API reference.
- Is there a feature-flag, beta, or plan-gating indicator on the account?

If `API State` genuinely is not in the product at our tier, that is a significant finding —
it would mean their most-marketed differentiator is either enterprise-gated or a
documentation pattern (expose an endpoint, stuff the result into simulation metadata,
assert on it with `METRIC_METADATA_FIELD`). Establish which, with evidence.

Then map the rest of the surface:

- Dump the **full `API State` configuration schema**: endpoint, method, headers, body,
  expected value, response path, timeout, retries, auth. Anything else?
- Can the expected value be **templated** from the test case (e.g.
  `{{expected_output.balance}}`)? What is the full template variable namespace available?
- **Is there any setup/seed hook** that Coval itself invokes before the call — a
  `before_call`, `setup`, `fixture`, or pre-simulation webhook? Their HTTP-first WebSocket
  mode lets a customer endpoint do work before returning a session URL; is that the only
  seam, or is there a real fixture API?
- **Is there any teardown, rollback, cleanup or reset hook** after verification? Search the
  OpenAPI schema names for `teardown`, `cleanup`, `rollback`, `reset`, `fixture`, `setup`,
  `before`, `after`. Report what exists and what does not.
- Is there any notion of **test isolation** — a namespace, tenant, sandbox or transaction
  per test run — or does every simulation hit the same customer environment?
- Does anything in the product ever touch a **database directly**, or is every state
  interaction mediated by an HTTP endpoint the customer wrote?

Report this phase as a table: capability / present or absent / evidence / where I looked.

### Phase 4 — Simulations, personas, audio

- Test Set supports types `SCENARIO`, `TRANSCRIPT`, `AUDIO`, `SCRIPT`. Explain each: what
  input does it take, and what does a run of each actually do? I am especially interested
  in `TRANSCRIPT` and `AUDIO` — is this "replay a production call as a regression test"?
  If so, how does a production call become a test case, and how many clicks is it?
- **Persona**: dump the full schema. It reportedly controls voice, interruption patterns,
  background noise and personality. Give me every knob and its range. Is background noise a
  file, a category, an SNR value? Are interruptions probabilistic or scripted?
- How is the simulated caller driven — a fixed script, or an LLM reacting turn by turn?
  What ends a conversation? Is there a max-turn limit?
- What audio artifacts come out of a run: combined only, or per-speaker tracks? What format?
- Which agent connection methods are supported (Vapi, LiveKit, Pipecat, SIP, WebSocket,
  OpenAI Realtime, custom HTTP)? What does each require from the customer?

### Phase 5 — Mutations, scheduling, reports, dashboards

- **Mutation** is the primitive I most want to understand. It is described as a deep-merged
  JSON override applied at runtime, one active per agent/name pair. How is a mutation
  defined, how is a run pointed at one, and how are results compared across mutations? Is
  there a statistical significance treatment, or just side-by-side numbers? Show me a real
  A/B comparison view.
- **Run Template → Scheduled Run**: cron and rate-based. What is the minimum interval?
  What happens to results — alerting, thresholds, regression detection against a baseline?
- **Reports**: they have a skill for "multi-run reports and turning grouped results into
  action plans." What does a report actually contain? Screenshot or describe the structure.
- **Dashboards**: what widget types exist (chart, table, text — others)? What can a widget
  be bound to? Is it arbitrary metric-over-time, or a fixed set?
- **Human review**: they have a skill for computing agreement between human and machine
  labels and proposing improved metric prompts. Find this in the UI. How is a review queue
  populated, what does an annotator see, and what agreement statistic do they report —
  raw agreement, Cohen's kappa, something else?

### Phase 6 — The experience, honestly assessed

Walk the product as a new user would and tell me:

- Time from signup to a first meaningful result. What is the actual onboarding path?
- Where does the UI make something obvious that a spreadsheet or a CI log could not?
- What is genuinely well designed — be specific about screens and interactions, not
  adjectives.
- Where is it clumsy, slow, confusing, or obviously bolted on?
- What does a failing run look like? Can you get from a red result to the responsible turn,
  the responsible tool call, and the audio in that turn — and how many clicks?
- What would make you choose it, and what would make you leave?

### Output

Give me one markdown document with:

1. **Object model** — the corrected hierarchy plus full field/enum tables.
2. **Metric catalogue** — every metric, exact names, output types, config schemas.
3. **The state and fixture table** from Phase 3.
4. **Ten things worth copying**, each with a one-line reason.
5. **Five things deliberately not worth copying**, each with a one-line reason.
6. **The structural gaps** — capabilities their architecture makes hard or impossible,
   with the reasoning. Mark these `[INFERRED]` and say what would falsify each.
7. **Open questions** you could not resolve, and exactly what access or command would
   resolve them.

Do not soften anything. If their product is better than I think, say so plainly and say
where. An accurate map of a strong competitor is worth more to me than a flattering one.
