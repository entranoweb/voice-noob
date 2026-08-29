# Handoff — Synthiq test layer

**Written:** 29 August 2026
**Reason:** PR #8 merged; work continues in a fresh session.

Everything a new session needs to pick this up without re-deriving it. Read
[`../SYNTHIQ_PLAN.md`](../SYNTHIQ_PLAN.md) for the vision and the flywheel and
[`DECISIONS.md`](DECISIONS.md) for why the code is shaped the way it is. This
document is the state of play and the next move.

---

## 1. Where the code is

| Fact | Value |
| --- | --- |
| Merged | [PR #8](https://github.com/entranoweb/voice-noob/pull/8) → `voice-prod` as `081cc1c`, 29 Aug 2026 06:39 UTC |
| Size | 22 commits, 168 files, +21133 / −12966 |
| Green at merge | 609 backend tests, 283 frontend tests; ruff, format, `mypy --strict` (104 files), eslint, tsc, prettier |
| CI | `.github/workflows/` — Postgres 17 + Redis 7 services, import guard, single-Alembic-head assertion, backend and frontend gates |

`voice-prod` is the integration branch. Start new work from it.

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

Metrics currently registered: `conversation_valid_end`, `state_restored`
(validation); `task_completion` (accuracy); `transcription_accuracy`,
`time_to_first_audio`, `interruption_handling` (experience);
`tool_call_validity`, `response_speed` (diagnostic).

Outside the package: `POST /api/v1/testing/check` in `backend/app/api/testing.py`,
the promptfoo provider in `integrations/promptfoo/`, and the `fixture` JSON
column on `test_scenario` (migration `53b1ba24a87b`).

**Three model roles, three settings** (`backend/app/core/config.py`):
`QA_AGENT_MODEL` is the measurement and stays capable; `QA_CALLER_MODEL` and
`QA_JUDGE_MODEL` default to Haiku; `QA_OPEN_MODEL_BASE_URL` /
`QA_OPEN_MODEL_API_KEY` point caller and judge at any OpenAI-compatible endpoint
— a cost lever, and a requirement for running inside a customer's network.

## 3. The honest limits

State these before any claim built on the harness.

- **No call has ever gone through Telnyx.** Still true. The inbound path is now
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
| 5 | **One real call, end to end** — Telnyx number, live call, a row landed | Path fixed and covered end to end; **the live call itself still needs credentials, a number and a handset** | ½ day left |
| 6 | **Audio in the loop** — TTFB and barge-in wired to the live transport; WER wired but needs a scripted caller over audio to become measurable. Per-speaker tracks still to do | #5's live call | 1 week left |
| 7 | **Red team over a real line** — OWASP LLM Top 10 / EU AI Act mapping; the procurement artifact | #6 | 4–5 days |
| 8 | **Judge calibration** — human-vs-machine agreement, then prompt refinement | Human-labelled runs | 3 days |
| 9 | **Scheduled runs and reports** — cron run templates, regression against a baseline | nothing | 4–5 days |
| 10 | **The Synthiq rename** — repo, packages, API still say Voice Noob | nothing; cheap now, expensive after a design partner integrates | 1 day |

**#5's remaining half-day is still the critical path.** #6 and #7 both sit behind it, and #7 is what
converts the work into contracts. #9 and #10 are the only unblocked items, and
#10 gets more expensive every week it waits.

Also open, from §7 — decide before more code, not after: licence (MIT or
Apache-2.0, **not** BSL), monetisation (harness free and forkable; sell hosted
EU-region cloud), the OpenAI-acquired-promptfoo answer for security reviews, and
keeping the engine choice out of the test layer.

## 5. Next step for a new session

1. Branch from `voice-prod`.
2. Place the live call — see §7 for the exact procedure and what to check.
3. Then item **#6**'s remainder: a scripted caller speaking over audio, so
   `transcription_accuracy` has a reference to score against, and per-speaker
   tracks.

Do **not** start #8 — building calibration machinery with no labelled runs to
put through it is scaffolding, not progress.

## 6. What the inbound path was doing wrong

None of these were visible to any test, and each one alone was enough to lose a
real call. They are listed because they are the argument for §7: the next
unverified thing is the next five bugs.

| Defect | Effect on a real call |
| --- | --- |
| `/webhooks/telnyx/voice` called `request.json()` | A TeXML application — what the purchase flow configures, and the only mode in which returning a document does anything — posts form-encoded. The webhook raised, returned 500, and the carrier dropped the call. Both webhooks now read either shape via `services/telephony/telnyx_events.py` |
| `CallRecord(user_id=agent.user_id)` on both inbound webhooks | `call_records.user_id` is a UUID column; `Agent.user_id` is an integer. The insert raised against Postgres, so no inbound call had ever landed a row. Now goes through `user_id_to_uuid`, as the outbound path already did |
| `<Stream>` set no `bidirectionalMode` | Telnyx defaults it to `mp3`; this bridge sends G.711 µ-law. The caller would have heard silence while the logs showed audio being written. Now `rtp` / `PCMU` / `8000`, with a `<Pause>` so the document does not end under the stream |
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

Everything below the carrier is covered. This is what is not.

1. Put `TELNYX_API_KEY` and `TELNYX_PUBLIC_KEY` in the environment. Never commit
   them. `TELNYX_PUBLIC_KEY` is required — with none set, signature verification
   falls back to `settings.DEBUG`, which must be false in production.
2. Point a number at `https://<host>/webhooks/telnyx/voice` as a **TeXML
   application** (`configure_phone_number_webhook` does this), with the status
   callback at `/webhooks/telnyx/status`. The host must be publicly reachable
   over TLS: the answer document hands Telnyx a `wss://` URL derived from
   `request.base_url`, so behind a proxy the app needs the forwarded-proto
   headers or the URL will come out `ws://` and the stream will never connect.
3. Create an agent with `phone_number_id` set to the number in E.164, and
   `is_active` true. An inactive agent closes the websocket with 4003.
4. Call it. Then check, in this order:
   - a `call_records` row exists with that `CallSid` and status `completed`;
   - `turns` on that row is non-null;
   - `GET /api/v1/calls/{id}/metrics` reports a measured
     `time_to_first_audio` and `interruption_handling`, and
     `transcription_accuracy: null` (correct — a human caller has no script);
   - a `voice.call` span with `voice.turn` (and `voice.tool_call`) children.
     Set `OTEL_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT` to an OTLP/HTTP
     collector; `app/monitoring/tracing.py` installs the provider at startup and
     logs `tracing_configured` when it takes. With `OTEL_ENABLED` false the
     emitter is a no-op by design and the log says so.
5. Write down what actually happened, including the parts that did not work.

Standing constraint for any agent session: never claim a capability the code
does not have. The reason this project exists is that the previous plan called
the framework "85% production-ready" when it did not import.
