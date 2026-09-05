# Handoff — Synthiq test layer

**Written:** 29 August 2026
**Reason:** PR #11 merged; work continues in a fresh session.

Everything a new session needs to pick this up without re-deriving it. Read
[`../SYNTHIQ_PLAN.md`](../SYNTHIQ_PLAN.md) for the vision and the flywheel and
[`DECISIONS.md`](DECISIONS.md) for why the code is shaped the way it is. This
document is the state of play and the next move.

---

## 1. Where the code is

| Fact | Value |
| --- | --- |
| Merged | [PR #11](https://github.com/entranoweb/voice-noob/pull/11) → `main` as `070b132`, 29 Aug 2026 21:17 UTC |
| Size | 15 commits, 33 files, +4970 / −149 |
| Green at merge | 729 backend tests, 283 frontend tests; ruff, format, `mypy --strict` (109 files), eslint, tsc, prettier |
| Alembic | single head `b83d1c4f7a90` |
| Before that | [PR #8](https://github.com/entranoweb/voice-noob/pull/8) as `081cc1c`, 29 Aug 06:39 UTC — the harness itself |
| CI | `.github/workflows/ci.yml` — Postgres 17 + Redis 7 services, import guard, single-Alembic-head assertion, backend and frontend gates |

`main` carries everything. Branch from it.

**Reviews on PR #11 ran to seven rounds.** Worth knowing why, because the pattern
repeats: the first rounds found real defects in the diff, and the last three each
found a defect introduced by the *previous* fix — all inside
`monitoring/tracing.py`. If that file misbehaves, it is the youngest and most
churned code in the merge. Two of those rounds also caught claims that were
wrong rather than code that was: a metric category read off a directory instead
of the class, and a "verified by reverting" claim that held for one of two tests.
Verify, then claim.

## 2. What exists now

The harness lives in `backend/app/services/qa/`.

| File | What it does |
| --- | --- |
| `test_runner.py` | Runs a scenario: caller turn → agent turn (real Anthropic tool-use loop) → metrics. Owns the fixture scope ordering |
| `tool_binding.py` | Converts the agent's own registry tools to Anthropic schema and **executes them for real**. External integrations excluded by passing no credentials |
| `fixtures.py` | Seeds, isolates and provably rolls back a run's database state. `join_transaction_mode="create_savepoint"` is the load-bearing line |
| `testing.py` | `ScenarioSpec`, `RunResult`, `ScenarioChecker.run/check/compare` — the pytest-native API. A scenario is an argument, not a row |
| `mutations.py` | Deep-merged config overrides, Wilson score intervals, `Comparison.winner()` returning `None` when intervals overlap |
| `caller.py` | Adaptive persona-driven caller. Refuses to volunteer information or be unusually patient |
| `resilience.py` | Circuit breaker + retry around the model calls |
| `evaluator.py` | Cost accounting; `self-hosted/` prefixed models cost zero |
| `metrics/` | Four layers: `validation/` gates, `accuracy/` decides, `experience/` and `diagnostic/` report |

Metrics currently registered — ten, asserted as an exact set in
`tests/test_services/test_metrics/test_runner.py`: `conversation_has_turns`,
`conversation_valid_end`, `state_restored` (validation); `task_completion`,
`expected_tools_invoked` (accuracy); `transcription_accuracy`,
`time_to_first_audio`, `interruption_handling` (experience);
`tool_call_validity`, `response_speed` (diagnostic).

Read the category off the class, not the directory: `expected_tools_invoked`
lives in `metrics/diagnostic/tool_call_validity.py` beside the metric it shares
a parser with, and is registered `ACCURACY`. The distinction decides whether it
decides — accuracy sets the verdict, diagnostic only reports (§3 of
[`DECISIONS.md`](DECISIONS.md)).

Outside the package: `POST /api/v1/testing/check` in `backend/app/api/testing.py`,
the promptfoo provider in `integrations/promptfoo/`, and the `fixture` JSON
column on `test_scenario` (migration `53b1ba24a87b`).

**The live-call side**, added by the inbound-path work in §6. The harness above
measures simulations; these are what produce the data for a call that actually
happened.

| File | What it does |
| --- | --- |
| `monitoring/audio_turns.py` | `AudioTurnRecorder` — rebuilds conversational turns from the live bridge: speech boundaries, first audio byte out, barge-ins, tool calls. Transport-agnostic, and it owns the clock. This is what feeds the three audio metrics |
| `monitoring/call_trace_emitter.py` | `CallTraceEmitter` — the producer for the `voice.call` / `voice.turn` / `voice.tool_call` tree `call_trace.py` had only ever *defined*. Talks to the OTel API only |
| `monitoring/tracing.py` | `configure_tracing` / `shutdown_tracing` — installs the SDK provider and OTLP/HTTP exporter at startup from `main.py`'s lifespan, flushes on shutdown. Without it the emitter is a no-op everywhere |
| `services/telephony/telnyx_events.py` | `parse_telnyx_webhook` — normalises Call Control JSON and TeXML form-encoded into one `TelnyxCallEvent` |
| `services/qa/call_metrics.py` | `metrics_for_call` — the metric suite over a real call's persisted turns, scoped to what a non-simulation can answer |
| `api/calls.py` | `GET /api/v1/calls/{call_id}/metrics`, scoped to the calling user's own records |
| `tests/websocket/asgi_ws.py` | Websocket client that speaks ASGI in the test's own event loop, so a websocket test can share the asyncpg fixtures. `TestClient` cannot: it drives the app from a second thread |

`call_records` gained two nullable columns: `turns` (migration `7c4e91f0ab12`)
and `termination_reason` (migration `b83d1c4f7a90`). Both are null-means-unknown,
not null-means-zero — see DECISIONS §1, §28. Tracing adds one runtime dependency,
`opentelemetry-exporter-otlp-proto-http`.

**Transcript retention is honoured on this path.** An agent with
`enable_transcript` false sets `retain_text=False` on the recorder, which drops
the turn text *and* the tool arguments and tool errors — see DECISIONS §32.

**Three model roles, three settings** (`backend/app/core/config.py`):
`QA_AGENT_MODEL` is the measurement and stays capable; `QA_CALLER_MODEL` and
`QA_JUDGE_MODEL` default to Haiku; `QA_OPEN_MODEL_BASE_URL` /
`QA_OPEN_MODEL_API_KEY` point caller and judge at any OpenAI-compatible endpoint
— a cost lever, and a requirement for running inside a customer's network.

## 3. The honest limits

State these before any claim built on the harness.

- **No call has ever gone through Telnyx.** Still true after PR #11. The inbound path is now
  exercised end to end against the real application — signed webhook, TeXML
  document, websocket, µ-law frames both ways, a row in Postgres, spans out —
  with only the model stubbed (`backend/tests/websocket/test_telnyx_inbound_call.py`).
  That is not the same as a carrier and a handset. Five defects that would each
  have dropped or silenced a real call were found and fixed *because* nothing
  had ever tried; see §6. Nothing proves there is not a sixth.
- **The audio metrics now have a producer, and it is only exercised by a
  stubbed model.** `time_to_first_audio` and `interruption_handling` are
  measured off the bridge and land on the call record.
  `transcription_accuracy` stays `not_measurable` on a call from a human,
  because word error rate needs a script to compare against and a real caller
  has none. It becomes measurable when the caller is a simulation.
- **The judge is uncalibrated.** Nothing measures whether it agrees with a
  human, so its scores are opinion. It is off by default in `check()` for
  exactly that reason.
- **Competitive claims are partly inferred.** `SYNTHIQ_PLAN.md` §8 lists what is
  unverified — Coval's fixture gap is inferred from an API omission, not proven.
  Do not put it in a deck before running
  [`COVAL_RECON_PROMPT.md`](COVAL_RECON_PROMPT.md).

## 4. What is pending

Ordered by value, from `SYNTHIQ_PLAN.md` §6.

| # | Work | Blocked on | Size |
| --- | --- | --- | --- |
| 5 | **One real call, end to end** — Telnyx number, live call, a row landed | Nothing in the code. **Credentials, a number and a handset** — see §7 | ½ day |
| 6 | **Audio in the loop** — TTFB and barge-in are wired to the live transport and land on the call record. WER is wired but needs a scripted caller speaking over audio to become measurable. Per-speaker tracks still to do | #5's live call | 1 week left |
| 7 | **Red team over a real line** — OWASP LLM Top 10 / EU AI Act mapping; the procurement artifact | #6 | 4–5 days |
| 8 | **Judge calibration** — human-vs-machine agreement, then prompt refinement | Human-labelled runs | 3 days |
| 9 | **Scheduled runs and reports** — cron run templates, regression against a baseline | nothing | 4–5 days |
| 10 | **The Synthiq rename** — repo, packages, API still say Synthiq Voice | nothing; cheap now, expensive after a design partner integrates | 1 day |

**#5 is the critical path and is now blocked on nothing but access.** Every
defect between the carrier and the database has been fixed and covered; what
remains is a phone call. #6 and #7 sit behind it, and #7 is what converts the
work into contracts. #9 and #10 are the only unblocked items, and #10 gets more
expensive every week it waits.

One more item, unnumbered because it is not product work: **the telephony
websockets are unauthenticated** (§6). It is the largest known hole in the
inbound path and it predates all of this. Anyone who learns an agent's UUID can
talk to that agent with its tools. Do #5 first — it is half a day and it gates
everything — but do not put a real number in front of the public internet
without deciding about this one.

Also open, from `SYNTHIQ_PLAN.md` §7 (not §7 of this document) — decide before
more code, not after: licence (MIT or
Apache-2.0, **not** BSL), monetisation (harness free and forkable; sell hosted
EU-region cloud), the OpenAI-acquired-promptfoo answer for security reviews, and
keeping the engine choice out of the test layer.

## 5. Next step for a new session

1. Branch from `main`.
2. **Place the live call** — §7 has the exact procedure and what to check, in
   order. This is the whole next step. Everything below it is blocked on it, and
   the value of doing it is not the green tick: it is that the last time
   something on this path was tried for the first time, it produced five defects
   that no test had seen.
3. Write down what happened, including the parts that did not work. Then item
   **#6**'s remainder: a scripted caller speaking over audio, so
   `transcription_accuracy` has a reference to score against, and per-speaker
   tracks.

Do **not** start #8 — building calibration machinery with no labelled runs to
put through it is scaffolding, not progress.

Do **not** treat #5 as done because the tests pass. They cover everything below
the carrier; they do not cover the carrier.

## 6. What the inbound path was doing wrong (fixed in PR #11)

None of these were visible to any test, and each one alone was enough to lose a
real call. They are listed because they are the argument for §7: the next
unverified thing is the next five bugs.

| Defect | Effect on a real call |
| --- | --- |
| `/webhooks/telnyx/voice` called `request.json()` | A TeXML application — what the purchase flow configures, and the only mode in which returning a document does anything — posts form-encoded. The webhook raised, returned 500, and the carrier dropped the call. Both webhooks now read either shape via `services/telephony/telnyx_events.py` |
| `CallRecord(user_id=agent.user_id)` on both inbound webhooks | `call_records.user_id` is a UUID column; `Agent.user_id` is an integer. The insert raised against Postgres, so no inbound call had ever landed a row. Now goes through `user_id_to_uuid`, as the outbound path already did |
| `<Stream>` set no `bidirectionalMode` | Telnyx defaults it to `mp3`; this bridge sends G.711 µ-law. The caller would have heard silence while the logs showed audio being written. Now `rtp` / `PCMU` / `8000`. The document is `<Connect><Stream/></Connect><Hangup/>` — an earlier version carried a `<Pause length="40"/>` after the stream, which ended every call with forty seconds of dead air; see DECISIONS §31 |
| Nothing sent `{"event": "clear"}` on barge-in | Server VAD cancels the response upstream, but Telnyx keeps playing what it has already buffered. The agent talked over the caller. This is also what `interruption_handling` measures |
| `log.info(..., event=...)` in the status callback | `event` is structlog's own key for the message. The call raised inside the logger *after* the commit, so the status webhook returned 500 to Telnyx on every hangup |

One thing this pass did **not** fix, and a new session should know about: the
telephony websockets are unauthenticated. Anyone who learns an agent's UUID can
open `/ws/telephony/{provider}/{agent_id}` and hold a conversation with that
agent, tools included. That predates this work and is architectural — a signed,
short-lived token minted by the answering webhook and checked at the socket is
the shape of the fix — but it is the largest known hole in the inbound path.
The database writes the bridge makes are now scoped to the serving agent, which
narrows the blast radius without closing the hole.

Also new, and previously absent entirely: **nothing emitted the OTel span tree**.
`monitoring/call_trace.py` defined `voice.call` / `voice.turn` / `voice.tool_call`
and had no producer; `monitoring/call_trace_emitter.py` is now that producer, the
bridge writes a tree per call, and `monitoring/tracing.py` installs the SDK so the
spans actually leave the process.

**A real call is measured, not graded.** `metrics_for_call` reports the outcome
`observed` rather than passed or failed, via `evaluate_observed`. The scenario
runner errors when no accuracy metric was measurable — correct for a scenario,
where it means the harness misbehaved — but a real call has no scenario, so
`task_completion` is unmeasurable by construction and that gate would stamp every
genuine call untrustworthy and throw away the latency numbers with it. The
validation gates still apply, and a call whose ending was never recorded fails
them, which is the honest reading: we do not know how it ended.

## 7. Placing the live call

Everything below the carrier is covered. This is what is not. Ordered so that
each step fails loudly rather than silently.

### 7.1 What must be deployed

The sandbox a coding session runs in is not publicly reachable, so this needs a
real deployment. Telnyx must be able to POST *into* it.

| Requirement | Detail |
| --- | --- |
| Public HTTPS host | Telnyx posts to it, and the media stream connects back to it. See 7.2 — this is the step most likely to fail |
| `PUBLIC_URL` | The public origin, e.g. `https://voice.example.com`. It is what the answer document's stream URL is built from. See 7.2 |
| Postgres 17, Redis 7 | `docker-compose.yml` has both. Run `alembic upgrade head`; the head is `b83d1c4f7a90` |
| `TELNYX_PUBLIC_KEY` | **Required.** Signature verification reads the *global* setting, not per-user credentials. With it unset, `validate_telnyx_signature` returns `settings.DEBUG` — so with `DEBUG=false` every webhook is rejected 403, and with `DEBUG=true` every webhook is accepted unverified. Neither is what you want by accident |
| `OPENAI_API_KEY` | The bridge opens a Realtime session with `gpt-realtime-2025-08-28` |
| `DEBUG=false` | See above |
| `TELNYX_API_KEY` | Only needed for outbound calls and the number-purchase flow. The inbound path does not use it |

Health: `/health`, `/health/db`, `/health/redis`, `/health/ready`, `/health/live`.
Check `/health/ready` before dialling — a failed Redis or Postgres shows up
there rather than in the middle of a call.

### 7.2 The stream URL host

`build_telnyx_stream_url` takes its origin from `PUBLIC_URL`, falling back to
`request.base_url` when it is unset. Set it. `request.base_url` is assembled
from the request line and the Host header, so behind a proxy that does not
forward them it resolves to the address the app is bound to, and the answer
document hands Telnyx `wss://internal:8000/...`. The webhook returns 200, the
document is well-formed, Telnyx accepts it, the `call_records` row lands — and
the media stream connects to nothing, so the caller hears silence while every
log line reads healthy.

**It is the host that goes wrong, never the scheme.** An earlier revision of
this section described this as a forwarded-proto trap that emits `ws://`, and
prescribed grepping the answer document for it. That check cannot fail: the
builder maps both `http` and `https` to `wss`, so the URL always says `wss` no
matter which host is in it. The same blind spot was in the test suite, whose
only assertion about the URL was `startswith("wss://")`; it asserts the host now
(DECISIONS §37).

Run uvicorn with `--proxy-headers --forwarded-allow-ips='<proxy IP>'` anyway —
client IPs and redirects still depend on it — but it is no longer what stands
between you and a silent call.

### 7.3 Wiring

1. Point the number at `https://<host>/webhooks/telnyx/voice` as a **TeXML
   application**, status callback `https://<host>/webhooks/telnyx/status`.
   TeXML, not Call Control: only TeXML consumes a returned document. Call
   Control posts JSON and expects API commands back, and while
   `parse_telnyx_webhook` reads either shape, the document is ignored in that
   mode and the call goes nowhere.
2. Create an agent with `phone_number_id` set to the number in E.164 and
   `is_active` true. The lookup matches with or without the leading `+`. An
   inactive agent closes the websocket with 4003; an unmatched number gets a
   spoken "no agent is configured for this number".
3. Set `enable_transcript` deliberately. False redacts turn text, tool arguments
   and tool errors — correct for privacy, but it will make the call look emptier
   than it was when you go looking at `turns`.

### 7.4 Preflight

Everything above, checked against the deployment itself, from a host that can
reach its database and Redis:

```
make preflight ARGS="--url https://voice.example.com"
```

It reports on the credentials, `PUBLIC_URL`, the migration head, Redis, the
agent's number, the public host, and then opens a real websocket to the stream
URL — the one thing no test could cover before a live host existed. That last
check uses a random agent id, which the bridge closes with 4004 after it has
resolved the route, accepted the upgrade and queried for the agent: TLS, the
proxy's websocket handling, routing and the database session, proven in one
connection, with no call placed and no row written. A proxy that will not
forward the upgrade answers with an HTTP status instead.

Exit status is 0 only when every check passed. A `warn` is a judgement call; a
`FAIL` is not.

### 7.5 Call it, then check in this order

| # | Check | Meaning if it fails |
| --- | --- | --- |
| 1 | A `call_records` row with that `CallSid`, status `completed` | The webhook or the status callback did not land |
| 2 | `turns` on that row is non-null | The websocket never carried audio. Suspect 7.2 first |
| 3 | `GET /api/v1/calls/{id}/metrics` reports a measured `time_to_first_audio` and `interruption_handling`, and `transcription_accuracy: null` | Null on the first two means no audio was recorded. **Null on the third is correct** — a human caller has no script to score against |
| 4 | Outcome is `observed`, not `passed`/`failed`/`error` | A real call is measured, not graded (§27) |
| 5 | A `voice.call` span with `voice.turn` (and `voice.tool_call`) children | See 7.6 |

### 7.6 Traces, if you want them

Optional — the call works without any of this. Set `OTEL_ENABLED=true` and
`OTEL_EXPORTER_OTLP_ENDPOINT` to an OTLP/HTTP collector. The startup log says
`tracing_provider_installed`, which means the provider is installed and spans
are addressed there — **not** that anything arrived. With `OTEL_ENABLED` false
the emitter is a no-op by design and the log says so.

A trace carries transcripts, so plain `http://` to a non-loopback host is
refused: `tracing_refused_cleartext_endpoint`, and `configure_tracing` returns
false. Use `https://`, a loopback collector, or set
`OTEL_ALLOW_INSECURE_EXPORT=true` to state that the network is trusted.

### 7.7 Then write down what actually happened

Including the parts that did not work. The value of this exercise is not the
green tick — it is that the last time this path was tried for the first time, it
produced five defects no test had seen.

Standing constraint for any agent session: never claim a capability the code
does not have. The reason this project exists is that the previous plan called
the framework "85% production-ready" when it did not import.
