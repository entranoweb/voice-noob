# Synthiq Voice: product-readiness audit

**Date:** 25 November 2026
**Commit audited:** `56702fa` (`main` after pull request #24), fresh clone, read-only.
**Question:** could a second customer sign up, connect an agent, run tests and trust the result, without us in the room?

**Answer: no, not through the product as it stands. A pilot is possible if we run the command-line caller for the customer and hand them a report.**
The repository holds two products that do not touch: the upstream agent-builder dashboard with a text test harness, and the new `caller/` package. The dashboard's test button is broken and its harness does not test the agent a caller reaches. The caller works over a direct connection but has no screen, no onboarding path for someone else's recordings, and no graded phone run.

Excluded on instruction: the licence question, and work in flight (security leftovers, settings docs, rig fixes and scorecard, pull request #25 reset/read-back/chains/compare, Retell and Vapi adapters). Where a matrix row depends on in-flight work it says "in flight, not on main".

How each claim was checked:
- **Code:** read at the cited `file:line` in the clone.
- **Tests run locally:** `caller/` 100 passed; frontend 283 passed (16 files). The backend suite, run locally the way `make test` runs it (no test database set, so it falls back to SQLite), ended with 21 failures; the backend figures below are from CI run 45446749176 on `main` (923 passed, 56% line coverage, Postgres). See F9.
- **Routes:** every route was dumped from the application's own API schema (137 routes) and compared with every path the frontend calls (99 distinct calls).
- **Live calls:** none placed by this audit. "Works end to end" for the caller rests on the local tests plus the stored live run files and the README's recorded output, over the direct connection only.

---

## 1. Completion matrix

States: **works** (end to end) · **tests only** (built, proven only by tests) · **never live** (built, never exercised on a real call or deployment) · **partial** · **stub** · **absent**.

### Totals (61 promised items)

| State | Count |
| --- | --- |
| Works end to end | 12 |
| Built, tests only | 11 |
| Built, never exercised live | 6 |
| Partial | 8 |
| Stub or dead | 2 |
| Absent | 22 |

Of the 12 that work, 5 are the caller over a direct connection, 6 are dependency, CI and documentation changes, and 1 is the batch runner on lookup-only scenarios. Nothing that involves a telephone line, a deployment or a customer-facing screen is in the "works" row.

### A. The original list (August 2026, `SYNTHIQ_PLAN.md` §6, `docs/HANDOFF.md` §4)

| # | Item | Claimed | Actual | Evidence | Left to call it complete |
| --- | --- | --- | --- | --- | --- |
| 1 | One real call, end to end | Blocked on credentials | **Never live.** Inbound path and caller phone leg both exist; no carrier call from either | `docs/HANDOFF.md:27`; `caller/README.md:255-257` | Place one call; write down what broke |
| 2 | Audio in the loop with a scripted caller | Not started (Aug) | **Works** over the direct connection | `caller/src/synthiq_caller/director.py`; 100 tests pass locally; stored run files | Same over a phone line |
| 3 | Separate recording of each side | Not started | **Never live.** The caller asks Twilio for a two-channel recording; the backend bridge captures no audio | `caller/src/synthiq_caller/twilio_rest.py:146`; `docs/ROADMAP-2027.md:90` | First phone call; confirm two channels come back |
| 4 | Red-team pack over a real line | Not started | **Absent.** No red-team, OWASP or injection scenario code in `backend/app` or `caller/src` | search, no match | Needs item 1 first |
| 5 | Judge calibration | Not started | **Absent.** Judge is off by default; roadmap treats it as overtaken by tool-result grading | `docs/HANDOFF.md:133-135` | Decide: drop it formally |
| 6 | Scheduled runs and regression against a baseline | Not started | **Absent on main** (baseline compare is in flight in #25; no scheduler anywhere) | search, no match | A scheduler and a stored baseline |
| 7 | Rename | Done (HANDOFF §0) | **Partial** | App name is renamed (`backend/app/core/config.py:35`), but README credits and clone URL are upstream's (`README.md:5,111`), database default is `voicenoob` (`config.py:54`), frontend package is `voice-agent-frontend` (`frontend/package.json:2`), README image link is broken (`README.md:7`; the file in the root is `voicenoob.png`) | One pass over README, compose and package names |

### B. `docs/ROADMAP-2027.md`

| # | Item | Claimed | Actual | Evidence | Left |
| --- | --- | --- | --- | --- | --- |
| 8 | Layer 1: route and contract tests | "Exists for own tools; contract pattern is new" | **Partial** | 923 backend tests; no offer-versus-book style contract test in this repo | Write the contract pattern once, as an example |
| 9 | Layer 2: text simulation | "Built and tested" | **Partial.** Passes in tests through the API; the dashboard run path is broken, it is off by default, and it simulates the agent on a different model (findings F2, F3) | `frontend/src/lib/api/qa.ts:674`; `backend/app/api/testing.py:672,686`; `backend/app/services/qa/test_runner.py:684-687` | Fix the button; say plainly what it tests |
| 10 | Layer 3: socket audio replay | Works, 18 calls | **Works** | `caller/README.md:65-79`; local tests | none |
| 11 | Layer 4: phone-leg replay | "Built and tested against a stand-in" | **Never live**, and a phone run is a single ungraded call outside the batch (finding F4) | `caller/src/synthiq_caller/cli.py:198,212`; `phone_server.py:147-150` | Phone leg in `batch`, trace lookup, grading, one call |
| 12 | Layer 5: production mining | Not built | **Partial.** Clip cutting and a scenario skeleton from one call exist; nothing fetches recordings, transcribes them or picks failed calls | `caller/src/synthiq_caller/clipbank/build.py:7`; `cli.py:240-248` | Fetch, transcribe, select |
| 13 | Clip replay with said, heard, stored | Working | **Works** over the direct connection. "Stored" is the tool's result, not a lookup | `caller/src/synthiq_caller/grading.py`; `caller/README.md:74-76` | Independent lookup (in flight) |
| 14 | Reset hooks | "Hook points only" | **Partial.** Before and after commands exist; no reset on main | `cli.py:225-226`; `caller/README.md:129-135` | #25, then one reset exercised live |
| 15 | Call chains | Next | **Absent on main** (in flight) | `caller/README.md:262` | #25 |
| 16 | Misbehaviour on demand | "Built and tested offline" | **Never live.** No shipped scenario uses `talk_over`, `stay_silent`, `delay_s` or keypad digits; the socket leg has no keypad channel | search of `caller/scenarios/**`: 0 uses; `caller/README.md:268-269` | One scenario per behaviour, run live |
| 17 | Trace adapters per engine | First adapter done | **Partial.** One (ElevenLabs) | `caller/src/synthiq_caller/traces/` | Two more are in flight, untested against real accounts |
| 18 | Parallel calls | "Still lack" | **Never live, untested.** A flag and a semaphore exist; no test mentions it | `cli.py:216`; `batch.py:225`; `caller/tests`: 0 matches | A test, then a live run of two |
| 19 | Release gating from a batch | "Still lack" | **Absent.** `batch` exits 0 whatever the pass rate | `cli.py:189,119` | Exit code from a threshold |
| 20 | Accent and noise personas | "Still lack" | **Absent** | search | later |
| 21 | Independent lookup of the stored record | Not built | **Absent on main** (in flight) | `docs/ROADMAP-2027.md:310-313` | #25 |
| 22 | Next clip chosen from what the agent said | Built | **Works** (pattern match on the agent's words) | `caller/scenarios/example-receptionist/03-consultation-single-slot-yes.json:17-131` | none |
| 23 | Hazard-log evidence export | Not built | **Absent** | search | later |
| 24 | Batch runner, N by k, Wilson intervals | 30-day item | **Works** for lookup-only scenarios | `caller/README.md:137-165` | Mutating scenarios (being run now) |
| 25 | Security hygiene: stream token, fail-closed webhooks, call-back host | 30-day item | **Tests only** | pull requests #18, #23 state no browser or carrier run | A rehearsal on a deployment |
| 26 | Cascaded pipeline beside the current bridge | 60-day | **Absent** | `README.md:138-142` | not started |
| 27 | Same flow on LiveKit Agents, with the talk-over regression test | 60-day | **Absent** | — | not started |
| 28 | Side-by-side numbers on real calls | 60-day | **Absent** | — | needs 26, 27 |
| 29 | One flow as a code-decided state machine with fixed lines, emergency pre-check, read-back and keypad | 60-day | **Absent** | — | not started |
| 30 | SIP leg for engine and caller | 90-day | **Absent** | — | not started |
| 31 | Hosted UK deployment with a data statement | 90-day | **Absent.** Nothing is deployed anywhere | — | not started |

### C. Archived December 2025 plans (`docs/archive/2025-12/`)

| # | Item | Claimed then | Actual | Evidence | Left |
| --- | --- | --- | --- | --- | --- |
| 32 | Post-call evaluation engine | "85% production-ready" | **Tests only.** Off by default; evaluator 46% covered, alerts 16% | `backend/app/core/config.py:181`; CI coverage | Decide whether it stays |
| 33 | Progress visibility over a websocket | Planned | **Absent.** The UI polls every 2 s | `frontend/src/components/qa/test-runner.tsx:170` | — |
| 34 | Test cancellation endpoint | Planned | **Absent** | route dump | — |
| 35 | Evaluation or call detail page | Planned | **Absent.** Three places link to it | finding F10 | Build the page or remove the links |
| 36 | Interactive charts | Planned | **Absent** (no chart library in `frontend/package.json`) | `frontend/package.json:22-58` | — |
| 37 | Scenario create, edit, delete | Planned | **Tests only** | `backend/app/api/testing.py:412-588`; frontend component tests | — |
| 38 | Batch evaluation | Planned | **Tests only.** Endpoint exists; nothing in the UI calls it | `POST /api/v1/qa/evaluate/batch`; `qa.ts` has no caller | — |
| 39 | PII redaction | Planned | **Absent** | search | — |
| 40 | Audit logging | Planned | **Stub.** `core/audit.py` exists, is imported nowhere, 0% covered | `backend/app/core/audit.py`; search | Wire it or delete it |
| 41 | CI integration | Planned | **Tests only.** `POST /api/v1/testing/check` and a promptfoo provider | `backend/app/api/testing.py:771`; `integrations/promptfoo/` | Run it once from a real pipeline |
| 42 | CSV or PDF export | Planned | **Absent** (transcript download only) | `frontend/src/app/dashboard/calls/page.tsx:150-153` | — |
| 43 | Distributed workers | Planned | **Absent.** In-process background task | `backend/app/api/testing.py:898` | — |
| 44 | Automatic test generation | Planned | **Absent** | search | — |
| 45 | A/B comparison of configurations | Planned | **Tests only.** No route, no screen | `backend/app/services/qa/mutations.py` | — |
| 46 | Pluggable metrics | Planned | **Tests only** | `backend/app/services/qa/metrics/registry.py` | — |
| 47 | Hardening: call registry, draining, Prometheus | "Complete" | **Tests only** | `backend/app/main.py:150-170,251` | A deployment |
| 48 | Call queue | "Sprint 2" | **Stub.** Disabled, imported nowhere | `config.py:229`; `backend/app/services/call_queue.py` | Delete or wire |

### D. Pull requests #12 to #24, headline claim

| # | Headline claim | Actual | Evidence |
| --- | --- | --- | --- |
| 49 | #12 two silent call failures fixed, preflight added | **Tests only.** Never run against a deployment | `docs/HANDOFF.md:120-126` |
| 50 | #13 deploy guide fixed, renamed to Synthiq Voice | **Partial** (rename leftovers, row 7; guide never followed on a real deployment) | row 7 |
| 51 | #14 unattended merges stopped, dependency scan, Next.js advisory | **Works** | `.github/workflows/ci.yml:114-149` |
| 52 | #15 roadmap and archive | **Works** (documents) | `docs/` |
| 53 | #16 auth libraries replaced, advisories cleared | **Works.** 923 tests green in CI. Note the backend advisory step is still report-only | `.github/workflows/ci.yml:141-149` |
| 54 | #17 engine-independent clip caller | **Works** over the direct connection | rows 2, 10, 13 |
| 55 | #18 call-path security | **Tests only** | row 25 |
| 56 | #19 frontend advisories cleared | **Works** (CI gate; not re-run here) | `ci.yml:128-136` |
| 57 | #20 batch runner, 20 scenarios, clip bank builder | **Partial.** Runner works on lookup-only scenarios; 6 of 20 scenarios had run live by the evening of 24 Nov, 2 cannot run (missing recordings), 12 change data and are only now being tried | `caller/README.md:199-206` |
| 58 | #21 zod 4, advisory check blocks | **Works.** 283 frontend tests pass locally | `ci.yml:134-136` |
| 59 | #22 phone leg, "ready for first real call" | **Never live**, and not usable for a graded batch | finding F4 |
| 60 | #23 browser sockets, embed sessions, single-use tokens | **Tests only** | pull request body: "Not verified: any browser run or carrier call" |
| 61 | #24 settings docs, roadmap corrections, flaky test | **Works** (documents; the flaky test has one green run) | — |

---

## 2. Walking the product as a second customer

| Step | What happens | Verdict |
| --- | --- | --- |
| Find the product | The README describes an AI phone-agent builder by its upstream author, tells you to clone the upstream repository, and never mentions testing or `caller/` | Wrong product described |
| Run it locally | Quick start fails three times before the first page loads (finding F9) | Broken |
| Sign up | Works. No email check, no password reset, no way back in if you forget it (F5) | Usable once |
| Create a workspace | Required before anything else; a manual step the dashboard prompts for | Works |
| Connect provider keys | The settings page opens on "All Workspaces (Admin)" and saves keys the call path never reads (F8). Keys are stored in plain text (F6) | Trap |
| Connect an agent built elsewhere | Not possible. An agent here is a prompt and a voice on this platform (F1) | Absent |
| Run a test from the dashboard | "Run Tests" posts to a route that does not exist (F2). If it were fixed, the test would run the prompt on a different model from the one that answers calls (F3) | Broken |
| Run a test with the caller | Command line only. Needs a clip bank cut from the customer's own recordings and word-level transcripts, and hand-written patterns for the agent's wording (F14) | Works for us, not for them |
| Test over the phone | One call at a time, ungraded (F4). No call placed yet | Not ready |
| See results | Text report in a terminal and JSON files. The dashboard links to a call page that does not exist (F10) | Thin |
| Pay | No plans, metering or limits (F7) | Absent |

---

## 3. Findings

Severity: **A** blocks a second customer · **B** costs us support time · **C** hygiene. Every finding is CONFIRMED in code unless marked PLAUSIBLE.

### A. Blocks a second customer

**F1. Two products in one repository, not joined.** CONFIRMED.
- Nothing in `backend/app`, `backend/tests`, `frontend/src`, the compose files or the Makefile refers to `caller/` (search: one unrelated comment, `backend/app/services/qa/metrics/context.py:17`).
- The agent model has no field for an agent on another platform (`backend/app/models/agent.py:27-139`: prompt, voice, tier, tools, phone number).
- So the dashboard cannot start, schedule or show a caller run, and the caller has no accounts, workspaces or history.
- Outside pattern: Cekura's first step is "select ElevenLabs as the provider", enter the agent id and an API key, choose the API region, and it imports name, prompt, tools and phone number; results appear in a dashboard (https://docs.cekura.ai/documentation/integrations/elevenlabs/testing.md, read 25 Nov 2026). promptfoo ships a command line plus a local results viewer and share links (`promptfoo view`, https://www.promptfoo.dev/docs/usage/web-ui/, read 25 Nov 2026). A command-line tool with a viewer is an accepted shape; a dashboard that cannot see the tool's results is not.

**F2. The dashboard's "Run Tests" button cannot work.** CONFIRMED.
- The frontend posts to `POST /api/v1/testing/runs` with `{agent_id, scenario_ids}` (`frontend/src/lib/api/qa.ts:673-689`, used by `frontend/src/components/qa/test-runner.tsx:183,220`).
- The backend serves only `GET` at that path (`backend/app/api/testing.py:913`). Running is `POST /run` for one scenario (`:672`) or `POST /run-all`, which returns no run id (`:844`, response model `:201-206`). The request gets 405.
- The shapes also disagree: the frontend expects a run with `total_scenarios`, `passed_scenarios` and `results[]` (`qa.ts:148-160`); the backend's run is one scenario with `overall_score` and `passed` (`testing.py:88-104`).
- It is hidden because the frontend's mock server answers the non-existent route (`frontend/tests/mocks/handlers.ts:226`), so the component tests pass.
- Route comparison result: 99 distinct frontend calls, this is the only one without a backend route.

**F3. A pass in the text harness is not a pass for the agent a caller reaches.** CONFIRMED.
- The simulated run sends the agent's system prompt to `settings.QA_AGENT_MODEL`, an Anthropic model (`backend/app/services/qa/test_runner.py:684-687`; `backend/app/core/config.py:200`).
- Real calls run on OpenAI speech-to-speech (`README.md:138-142`).
- So the harness tests "this prompt and these tools on another model, in text". It says nothing about hearing, turn-taking or the deployed model's behaviour.
- It is also off by default and cannot be switched on by a customer: `QA_ENABLED` defaults to false (`config.py:181`), the model key is read from the server's environment only (`backend/app/services/qa/resilience.py:45-52`; `testing.py:686-690`), and the page tells the user to "Contact your administrator" (`frontend/src/app/dashboard/qa/page.tsx:142-153`).

**F4. A phone run cannot be graded or batched.** CONFIRMED.
- `run`, `batch` and `regrade` accept one leg: `--leg elevenlabs-socket` (`caller/src/synthiq_caller/cli.py:198,212,232`).
- The phone leg has its own command, which runs one scenario and writes `phone-run-NNN.json` holding the timeline and Twilio's recording list (`caller/src/synthiq_caller/phone_server.py:93-98,147-150`). That file contains no trace and no grade (no reference to either in `phone_server.py`).
- `report` reads `run-*.json` only (`cli.py:110`), so phone runs never reach a report.
- The call reference a phone run keeps is Twilio's, not the engine's, and nothing maps one to the other.
- Consequence: the planned 20-call script over a real line, three rounds, cannot be produced by the current code even after the first call succeeds.
- Outside pattern: Coval documents the same problem and its answer: over the public network custom headers are stripped, so calls are matched "by caller number or timestamp" (https://docs.coval.ai/concepts/agents/connections/inbound-voice, read 25 Nov 2026).

**F5. No account lifecycle.** CONFIRMED.
- The only auth routes are register, login and me (route dump; `backend/app/api/auth.py:118,161,198`). No password reset, no password change, no email verification, no logout or revocation.
- The access token lasts 30 minutes and there is no refresh route although a refresh setting exists (`config.py:97-98`); any 401 clears the token and reloads to the login page (`frontend/src/lib/api.ts:61-73`). A user is signed out mid-task every half hour.
- `is_active` and `is_superuser` are stored and never read (`backend/app/models/user.py:23-24`; `auth.py:181-195`; `backend/app/core/auth.py:53-59`). An account cannot be disabled.
- Registration is open to anyone who can reach the host (`auth.py:118-119`, rate limit only).
- On a fresh database the app creates `admin@example.com` with the password from `ADMIN_PASSWORD`, whose default is a published string (`config.py:134-135`; `backend/app/main.py:97-106`). The start-up check that refuses a placeholder signing key does not cover it.
- A workspace belongs to exactly one user (`backend/app/models/workspace.py:37`); there are no members or invitations, so two people at one customer cannot share work.
- Outside pattern: the FastAPI project's own full-stack template refuses inactive users at login and on every request, and ships password recovery (https://github.com/fastapi/full-stack-fastapi-template/blob/master/backend/app/api/routes/login.py lines 31-36 and 85-90; `backend/app/api/deps.py` line 44). OWASP's Forgot Password cheat sheet gives the token rules (https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html).

**F6. Customers' provider keys are stored in plain text.** CONFIRMED.
- Columns for OpenAI, Azure, Deepgram, ElevenLabs, Telnyx and Twilio secrets are plain `Text` with the comment "encrypted at rest in production" (`backend/app/models/user_settings.py:34-71`); integration credentials likewise (`backend/app/models/user_integration.py:47`).
- There is no encryption code in the backend: a search for Fernet, encrypt, decrypt or pgcrypto finds only comments. The API writes the values straight to the row (`backend/app/api/settings.py:167-207`).
- Outside pattern: Dify encrypts each stored provider token per tenant before it reaches the database (https://github.com/langgenius/dify/blob/main/api/core/helper/encrypter.py, `encrypt_token`). OWASP's Key Management cheat sheet bars plaintext storage of keys (https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html).
- This is listed here, not left to the security work, because "paste your ElevenLabs key" is step one of onboarding and it is not in the in-flight list.

**F7. No billing, metering or limits, and the test endpoints spend our key without a rate limit.** CONFIRMED.
- No plan, usage, quota or payment code exists (search of `backend/app`, `caller/src`, `frontend/src`: the only matches are integration names and scenario text). `frontend/src/lib/pricing-tiers.ts` is the upstream's per-minute price display.
- `backend/app/api/testing.py` has no rate limit on any route (0 limiter decorators; `qa.py` has 15), and `/run`, `/run-all` and `/check` call the model with the server's key (`testing.py:686-690,788-792,855-859`).
- With open sign-up (F5), any registered user can run the server's model budget down.
- Outside pattern: usage is normally recorded per event against the customer and pushed to the biller, for example Stripe meter events in an open LLM evaluation platform (https://github.com/Agenta-AI/agenta/blob/main/api/ee/src/core/meters/service.py, around line 281).

**F8. The first key a new user saves is one the call path never reads.** CONFIRMED.
- The settings page starts on "All Workspaces (Admin)" (`frontend/src/app/dashboard/settings/page.tsx:252,300`) and saves with no workspace id.
- The call path reads workspace-level keys only, "no fallback to user-level settings" (`backend/app/api/settings.py:223-245`).
- Result: the key shows as set, and the first call fails for want of a key.

### B. Costs us support time

**F9. A new developer cannot run it from the README.** CONFIRMED, by following the steps on paper.
1. `git clone https://github.com/KenKaiii/voice-noob.git` clones the upstream project, not this one (`README.md:111`).
2. `cp backend/.env.example backend/.env` copies a placeholder `SECRET_KEY` with `DEBUG=false`, and the app refuses to start on it (`backend/.env.example:17,95`; `backend/app/core/config.py:255-266`). The README does not say to change it.
3. `docker-compose up -d` creates a database called `voice_agent` (`docker-compose.yml:9`); the copied settings point at `voicenoob` (`backend/.env.example:2`; default at `config.py:54`).
4. There is no migration step (`README.md:118-124`; the command exists as `make migrate`, `Makefile:55`). The start-up admin insert fails and is swallowed (`backend/app/main.py:114-115`), so the app starts and every data request fails.
5. Also stale: "Next.js 15" (it is 16.4.5, `frontend/package.json:48`), "the `deepgram-sdk` and `elevenlabs` packages are declared" (removed in #16), four model tiers of which two cannot place a call (`backend/app/api/realtime.py:218,470`), a broken image (`README.md:7`).
6. `run_tests.sh` calls `npm run test:run`, a script that does not exist (`run_tests.sh:101`; `frontend/package.json:5-21`).
7. `make test` runs the backend suite with no test database set (`Makefile:42-44`), so it uses the SQLite fallback (`backend/tests/conftest.py:57-70`). Run that way here, 21 tests failed, including the inbound-call end-to-end tests and the fixture rollback tests, while CI is green on Postgres. The fallback's own comment says it "fails tests that are correct". A newcomer's first test run is red. (Run on Windows; I did not separate SQLite causes from Windows causes.)
- The caller's own README is accurate and its commands work (`uv sync`, then `pytest`: 100 passed here). It is the one part a newcomer can run in one go.

**F10. Dead links and misleading numbers on the dashboard.** CONFIRMED.
- `/dashboard/calls/{id}` is linked from the dashboard, the call list and the evaluation list (`frontend/src/app/dashboard/page.tsx:208`; `calls/page.tsx:157`; `frontend/src/components/qa/evaluation-list.tsx:106`). No such page exists (pages under `dashboard/calls/`: `page.tsx` only).
- The measured call metrics endpoint, `GET /api/v1/calls/{call_id}/metrics`, has no caller in the frontend. The one honest measurement the backend produces is invisible.
- "Completed" and "Avg Duration" on the dashboard are computed from the five most recent calls and shown as totals (`dashboard/page.tsx:47,70-76`).
- Call search filters only the 20 rows already loaded (`calls/page.tsx:160-172`, comment "client-side for now").
- Three "coming soon" stubs: two model tiers on agent creation (`agents/create-agent/page.tsx:560`), OAuth integrations (`integrations/page.tsx:483`), workspace filter on phone numbers (`phone-numbers/page.tsx:442`).

**F11. The customer journey has almost no tests.** CONFIRMED.
- No backend test calls `/auth/register`, `/auth/login`, `/api/v1/workspaces` or `/api/v1/settings` (search of `backend/tests`: 0 matches). Password functions are unit-tested; the routes are not.
- CI coverage on `main`: 56% overall. On the journey: `api/workspaces.py` 29%, `api/settings.py` 39%, `api/agents.py` 53%, `api/embed.py` 37%, `api/telephony.py` 34%, `api/integrations.py` 34% (run 45446749176).
- CI sets no coverage floor (`.github/workflows/ci.yml:100-101`).
- Frontend tests cover the QA components, CRM, integrations and the sidebar. Login, register, agents, calls, settings and the browser test page have none (16 test files).
- By contrast the caller has 100 tests for about 30 source files, and they exercise the director, grading, batch guards and the phone leg against a stand-in.

**F12. CI does not check that the models and the migrations agree.** CONFIRMED as a gap; drift itself NOT CHECKED.
- CI asserts one migration head and that `upgrade head` applies to an empty database (`ci.yml:88-98`). It never compares the result with the models.
- The tests build their schema from the models, not from the migrations (`backend/tests/conftest.py:81,110`), so a missing migration cannot fail a test.
- Outside pattern: run `alembic check` straight after `upgrade head` (https://github.com/polarsource/polar/blob/main/.github/workflows/test_server.yaml around line 507; https://github.com/discord/access/blob/main/.github/workflows/db-migrations.yml around line 80).
- I could not run it: Docker was not running and I did not use the machine's own Postgres.

**F13. Ownership in the data model is split across two id systems with no foreign key.** CONFIRMED.
- `users.id` is an integer. Five tables store the owner as a UUID derived from it by hashing, with no foreign key: `call_records.user_id` (`backend/app/models/call_record.py:50`), `campaigns.user_id` (`campaign.py:83`), `phone_numbers.user_id` (`phone_number.py:48`), `user_settings.user_id` (`user_settings.py:25`), `user_integrations.user_id` (`user_integration.py:25`).
- Going back from the UUID to the user loads every user and hashes each (`backend/app/core/auth.py:62-82`).
- Deleting a user therefore leaves their calls, numbers, keys and campaigns behind, which matters for a deletion request.
- `contacts.user_id`, `appointments.contact_id` and `call_interactions.contact_id` have foreign keys with no delete rule (`contact.py:23`; `appointment.py:23`; `call_interaction.py:23`), so deleting a contact with history raises unless the code deletes children first.
- Two workspace columns are still marked "nullable for migration, will be required after data migration" (`contact.py:25`; `appointment.py:25`).

**F14. Nobody but us can set the caller up against their own agent.** CONFIRMED, except where marked.
- A clip bank needs recordings plus word-level transcripts with speaker ids (`caller/src/synthiq_caller/clipbank/build.py:7`; `caller/README.md:221-223`). Nothing in the package downloads recordings from a platform or transcribes them; on 24 November that was done with scripts that are not in the repository.
- A scenario is a list of patterns on the agent's exact wording, written by hand per agent (`caller/scenarios/example-receptionist/03-consultation-single-slot-yes.json:17-131`). There is no scenario reference page (`caller/docs/` holds the phone guide only).
- The ElevenLabs address is fixed to the US default; the constructors accept another but no command-line option passes one (`caller/src/synthiq_caller/legs/elevenlabs_socket.py:31,42`; `traces/elevenlabs.py:23,139`; `cli.py:196-234`). An account on EU residency cannot be tested without editing code. Cekura offers the region as a setting (source in F1).
- The only shipped scenario set is one client's, inside the product package (`caller/scenarios/example-receptionist/`).
- The package is not published or containerised; it is installed from the repository with `uv` (PLAUSIBLE as a pilot obstacle; fine for us).

### C. Hygiene

**F15. Dead code.** CONFIRMED by search for imports.
- `backend/app/services/qa/test_caller.py` (365 lines): exported from the package and used by nothing (`backend/app/services/qa/__init__.py:30`). The harness uses `caller.py`.
- `backend/app/core/audit.py` (244 lines) and `backend/app/services/base.py` (191 lines): imported nowhere, 0% covered.
- `backend/app/services/call_queue.py` (319 lines): imported nowhere in the app, feature flag off (`config.py:229`).

**F16. CI leftovers.** CONFIRMED.
- The backend advisory step is still report-only and its comment still says 85 advisories (`ci.yml:141-149`), although #16 reports none left. Its own deadline to block or delete is 8 December 2026.
- `backend/.env.example:33` pins the evaluation model to an older id than the code default (`config.py:183`). PLAUSIBLE as a fault: I did not check whether that id still resolves.

**F17. Accessibility and layout, at code level only.** CONFIRMED counts; impact PLAUSIBLE (nothing was rendered).
- Outside tests, `frontend/src` has 8 uses of `aria-label`, `aria-live`, `role="alert"` or `sr-only` in total, all inside shared primitives; the pages under `frontend/src/app` have none.
- Form errors are a plain `div` with no live region (`frontend/src/app/register/page.tsx:62-66`).
- Rows navigate through a click handler with no link, so they cannot be reached by keyboard (`calls/page.tsx:156-158`; `agents/page.tsx:338`).
- Status colours are fixed light-theme classes (`dashboard/page.tsx:226-232`).
- Layout: the shell is a flex row with a sidebar and responsive grids (`frontend/src/app/dashboard/layout.tsx:6-17`; `dashboard/page.tsx:115,190`). Nothing obviously fixed-width, but this was not checked at phone width.

---

## 4. What is sound

- **The caller's core.** Engine-neutral, free of client specifics (search of `caller/src` for client or agent names: only two usage examples in docstrings), 100 tests, guards before every call (mutation, name budget, hooks).
- **The route layer.** Of 99 distinct frontend calls, 98 have a matching backend route.
- **CI basics.** Import guard, lint, strict typing, single migration head, production build, advisory gates on the frontend, a separate job for the caller.
- **The harness's internals.** Fixture rollback, "not measurable" kept distinct from zero, intervals before naming a winner. The problem is what it is pointed at (F3), not how it is built.
- **Honesty of the documents.** `docs/HANDOFF.md`, `caller/README.md` and the corrected roadmap state their own limits accurately. The root README is the exception.

---

## 5. Shortest ordered list to a first external pilot

The pilot that is reachable is: **we run the caller against the customer's agent and give them a report.** No dashboard, no sign-up.

1. **Phone leg into the batch, with grading.** Add the phone leg to `batch`; find the engine's conversation for each call by time and number; grade and report it like a socket run (F4). Then place the first real call.
2. **Land #25 and exercise it live:** stored-record lookup, reset, one chain, on our own first agent.
3. **A pass-rate exit code and the HTML scorecard** (scorecard is in flight), so a run produces something to hand over and can gate a release (row 19).
4. **An onboarding kit for someone else's agent:** one command to fetch recordings and transcribe them into the clip-bank shape, the scenario skeleton from a call, a region option for the engine address, and a one-page scenario reference (F14).
5. **A volunteer clip bank with consent recorded per clip**, so the pilot does not depend on a customer's caller recordings.
6. **Replace the root README** with what the product is and a quick start that has been followed on a clean machine (F9). Move the client scenario set out of the package or label it as an example.

Only if the pilot must include the dashboard, add, in this order: fix or remove "Run Tests" (F2); hide the upstream CRM, campaign and number-purchase pages; password reset and the `is_active` check (F5); encrypt stored keys (F6); a per-workspace model key or a hard usage cap (F7); the settings default (F8). That is a second project, not a step.

---

## 6. Not checked

- No live call was placed; caller results rest on local tests and stored run files.
- The backend suite was not run locally against Postgres; the local SQLite run failed 21 tests (F9) and the pass count and coverage are from CI.
- Model-versus-migration drift (`alembic check`): no Postgres available to me.
- Nothing was rendered in a browser: accessibility and responsiveness are from code only.
- Docker images, `docker-compose.prod.yml` and `DEPLOYMENT.md` were not walked (in flight elsewhere).
- The promptfoo provider, the embed widget, campaigns and the CRM were not exercised beyond route matching.
- Whether the older evaluation model id in `.env.example` still resolves.
- Competitor and pattern sources were read on 25 November 2026 from public documentation and repositories; none was used hands-on. No project, owner or client name was sent to any outside service.
