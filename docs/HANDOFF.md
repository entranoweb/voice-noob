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

- **No call has ever gone through Telnyx.** Every result so far comes from a
  scripted model against a local Postgres. The three audio metrics exist and
  report `not_measurable` because nothing has fed them.
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
| 5 | **One real call, end to end** — Telnyx number, live call, a row landed | **Telnyx credentials + a phone number** | 1 day |
| 6 | **Audio in the loop** — wire the Media Streams driver so WER, TTFB and barge-in get real data; per-speaker tracks | #5 | 1–2 weeks |
| 7 | **Red team over a real line** — OWASP LLM Top 10 / EU AI Act mapping; the procurement artifact | #6 | 4–5 days |
| 8 | **Judge calibration** — human-vs-machine agreement, then prompt refinement | Human-labelled runs | 3 days |
| 9 | **Scheduled runs and reports** — cron run templates, regression against a baseline | nothing | 4–5 days |
| 10 | **The Synthiq rename** — repo, packages, API still say Voice Noob | nothing; cheap now, expensive after a design partner integrates | 1 day |

**#5 is the critical path.** #6 and #7 both sit behind it, and #7 is what
converts the work into contracts. #9 and #10 are the only unblocked items, and
#10 gets more expensive every week it waits.

Also open, from §7 — decide before more code, not after: licence (MIT or
Apache-2.0, **not** BSL), monetisation (harness free and forkable; sell hosted
EU-region cloud), the OpenAI-acquired-promptfoo answer for security reviews, and
keeping the engine choice out of the test layer.

## 5. Next step for a new session

1. Branch from `voice-prod`.
2. If Telnyx credentials are in hand → item **#5**: place one real call through
   the Telnyx driver, confirm the trace lands in the OTel schema, then item #6.
3. If not → item **#10** (the rename, one day, unblocked and decaying) or item
   **#9** (scheduled runs). Do **not** start #8 — building calibration
   machinery with no labelled runs to put through it is scaffolding, not
   progress.

Standing constraint for any agent session: never claim a capability the code
does not have. The reason this project exists is that the previous plan called
the framework "85% production-ready" when it did not import.
