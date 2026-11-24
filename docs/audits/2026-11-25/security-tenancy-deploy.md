# Synthiq Voice: security, tenancy and deployment audit (25 Nov 2026)

Scope: `entranoweb/voice-noob`, `main` at `56702fa` (after #24). Read-only. Nothing was executed, no host was probed, no live traffic was sent. Every finding was read in the code at the cited line. "CONFIRMED" means the code path is unambiguous on reading; "PLAUSIBLE" means the code allows it but the runtime effect was not exercised.

Lens: a product sold to several customers on one deployment, with a UK healthcare buyer in mind.

Not re-reported because it is already in flight: Telnyx signature per workspace, shared rate-limit storage, the embed `/token` instructions leak, `frame-ancestors`, compose env passthrough, settings docs.

## Verdict

The call path hardened in #18 and #23 is in good shape. The dashboard API and the deployment files are not ready for a second paying customer on a shared instance. Three things carry most of the risk:

1. Customer secrets (model, carrier and CRM keys) sit in the database in plain text.
2. A signed-in user can reach another customer's data or phone line through four routes that trust an id from the client, and anyone on the internet can sign up.
3. The data-protection features the dashboard reports as "complete" (export, erasure, retention, audit) are broken, partial or never run.

Counts: 7 block a paying deployment, 12 serious, 11 hygiene.

## Route table

139 routes in `backend/app/api` (script: AST walk of every router decorator).

| Group | Count | Result |
| --- | ---: | --- |
| No signed-in user by design | 27 | 2 auth, 7 health, 1 QA health, 2 static option lists, 6 embed (signed embed session), 6 carrier webhooks (signature), 3 sockets (login token or stream token) |
| Require a signed-in user | 112 | Query filters of every handler read |
| ...owner filter present and correct | 100 | |
| ...missing or partial ownership check | 7 | B2, B3, B5 below |
| ...dead on Postgres from the owner-id type split (fail closed) | 5 | S11 below |

The seven: `POST /api/v1/tools/execute`, `GET /api/v1/realtime/token/{agent_id}`, `POST /api/v1/realtime/session/{agent_id}`, `PUT /api/v1/phone-numbers/{id}`, `POST`/`PUT /api/v1/agents` (the `phone_number_id` field), `POST /api/v1/campaigns` (`workspace_id`), `POST /api/v1/compliance/retention/cleanup`.

## Blocks a paying deployment

### B1. Provider keys and integration credentials are stored in plain text. CONFIRMED
- `backend/app/models/user_settings.py:34-71`: OpenAI, Azure, Deepgram, ElevenLabs, Telnyx keys and the Twilio auth token are plain `Text` columns. The comment at line 34 says "encrypted at rest in production".
- `backend/app/models/user_integration.py:47-68`: `credentials` JSON and `refresh_token`, commented "encrypted".
- No encryption code exists anywhere in `backend/app` (search for `encrypt`, `Fernet`, `pgcrypto`: comments only).
- Effect: one database dump, backup or SQL injection exposes every customer's billing-bearing keys and CRM tokens.
- Outside pattern: Dify encrypts every tenant credential with a per-tenant key before it reaches the database (`langgenius/dify`, `api/core/helper/encrypter.py`, `encrypt_token(tenant_id, token)`). OWASP Secrets Management Cheat Sheet §7: "secrets must be stored encrypted", and §7.3: "You should not store keys next to the secrets they encrypt" (https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html).

### B2. One customer can take over or knock out another customer's inbound number. CONFIRMED (logic), PLAUSIBLE (runtime)
- `backend/app/api/agents.py:154` and `:353`: an agent's `phone_number_id` is free text from the request. It is not checked against the user's own numbers and has no uniqueness rule (`models/agent.py:124`).
- `backend/app/api/telephony.py:215-229`: inbound routing looks the dialled number up across all tenants and takes the first agent whose `phone_number_id` matches, before the assignment table.
- `telephony.py:898-899`: the webhook is then verified against the Twilio token of whichever agent matched.
- Effect on Twilio with per-workspace accounts: customer A sets their agent's `phone_number_id` to customer B's number; B's inbound webhooks now resolve to A's agent, fail signature and return 403. B's line is dead.
- Effect where both sit on the platform Twilio account, or on Telnyx with the global key: B's callers are answered by A's agent and the transcript lands in A's account.
- The only thing the attacker needs is the victim's phone number, which is public.
- `phone_numbers.py:45`: numbers are unique per user only, so two tenants can register the same number.

### B3. Four routes act on another customer's object from a client-supplied id. CONFIRMED
- `backend/app/api/tools.py:62-85`: `agent_id` from the body selects the workspace with no check that the agent is the caller's. `crm_tools.py:241-243, 369-370, 425-427, 535-536, 602-603, 653-654` then scope by that workspace only. A signed-in user can search, create, book, cancel and reschedule in another customer's contacts and appointments.
- `backend/app/api/realtime.py:613`, returned at `:700-706`: `GET /realtime/token/{agent_id}` loads any agent and returns its `system_prompt`, name, voice and tool list. This is the signed-in dashboard route, not the embed route already in flight.
- `backend/app/api/realtime.py:461`: `POST /realtime/session/{agent_id}` runs any agent's prompt with no ownership check.
- `backend/app/api/phone_numbers.py:398-403`: `assigned_agent_id` is written without checking the agent's owner; the response returns that agent's name.
- Agent ids are random UUIDs, so this needs a leaked id. They appear in webhook and stream URLs, logs, traces and metrics labels.
- Outside pattern: OWASP API1:2023, "check if the logged-in user has access to perform the requested action on the record" (https://api-security.owasp.org/editions/2023/en/0xa1-broken-object-level-authorization). The FastAPI reference template checks the owner on every by-id handler (`fastapi/full-stack-fastapi-template`, `backend/app/api/routes/items.py`).

### B4. The production compose file ships working default credentials. CONFIRMED
- `docker-compose.prod.yml:66-67`: `ADMIN_EMAIL` defaults to a fixed address and `ADMIN_PASSWORD` to `changeme`. `backend/app/main.py:97-106` creates that account as superuser on first start.
- `docker-compose.prod.yml:13, 32, 52, 54`: Postgres and Redis passwords default to `changeme_in_production`.
- `backend/app/core/config.py:134-135`: the app's own default admin password is a fixed string.
- Only `SECRET_KEY` is refused at start (`config.py:241-266`). The same refusal is missing for these three.

### B5. Export, erasure and retention do not work, and the dashboard says they do. CONFIRMED (code), PLAUSIBLE (runtime)
- `backend/app/api/compliance.py:581, 653, 843, 871`: `Agent.user_id` is an integer (`models/agent.py:30`) and is compared with a UUID. On Postgres with asyncpg this raises; the repository's own comment at `telephony.py:929-931` describes the mirror case raising. Export and erasure return 500 for any user. No test covers either route.
- `compliance.py:828-890`: even when fixed, erasure leaves phone numbers, campaigns, call evaluations, test runs and user-level integration credentials behind, and does not remove the account.
- `compliance.py:911-977`: retention clean-up exists only as a function behind `POST /retention/cleanup` (`:980-991`). Nothing schedules it. Any signed-in user can trigger it for every tenant.
- `compliance.py:214-217, 261-282`: the status endpoint hard-codes "complete" for encryption in transit, export, deletion and consent withdrawal.
- The consent it records is the account holder's (`:505-529`), not the caller's. The caller is the data subject, and there is no caller notice, per-caller export or erase-by-number.

### B6. There is no audit trail. CONFIRMED
- `backend/app/core/audit.py` defines audit logging and is imported nowhere.
- `backend/app/main.py:57-59`: outside `DEBUG` the log level is WARNING, so every `info` event (login success, key change, export, deletion, call start) is dropped.
- `backend/app/api/settings.py:132-213`: changing a provider key writes no log at all.
- Nothing is written to a table. A buyer asking "who changed this agent, who read this transcript" cannot be answered.

### B7. Anyone can create an account, and an account cannot be switched off. CONFIRMED
- `backend/app/api/auth.py:118-158`: open registration with no setting to disable it, no email verification and no password rule (`:35-40`, `password: str`).
- `backend/app/core/auth.py:53-59`: HTTP requests do not check `is_active`. The socket path does (`realtime.py:122`). A disabled user keeps full API access.
- No password change, reset, logout or token revocation route exists. Tokens carry `sub` and `exp` only (`auth.py:108`).
- Combined with B2 and B3, the attacker in those findings is anyone on the internet.
- Outside pattern: `fastapi/full-stack-fastapi-template`, `backend/app/api/deps.py`, refuses inactive users in the shared dependency.

## Serious

### S1. On phone calls the per-tool selection is ignored and tool calls are not checked. CONFIRMED
- `backend/app/services/gpt_realtime.py:307`: the phone path calls `get_all_tool_definitions(enabled_tools)` without `enabled_tool_ids`. `registry.py:153-155` then returns every tool in the group. An owner who enabled only "search" also exposes cancel and reschedule to callers.
- `gpt_realtime.py:424` and `registry.py:207-338`: whatever tool name the model emits is executed. The embed path checks the name against the enabled set (`embed.py:739-757`); the phone path and `/tools/execute` do not.

### S2. Tools trust whatever the caller says. CONFIRMED
- `backend/app/services/tools/crm_tools.py:533-555`: appointments are listed for any phone number spoken.
- `crm_tools.py:600-605, 651-656`: an appointment is cancelled or moved by id with no link to the caller.
- No caller identity step exists in the engine. A caller, or text injected through speech, can read or change another person's bookings. This is the same class of fault the client's own receptionist is being fixed for.

### S3. Tenant-supplied URLs are fetched by the server with no destination check. CONFIRMED
- `backend/app/services/qa/alerts.py:55-56, 150-151, 203-204`: `qa_alert_webhook` and `qa_slack_webhook` come from the free-form workspace `settings` dict (`workspaces.py:62, 93`) and are POSTed to as given.
- `backend/app/api/settings.py:182` and `gpt_realtime.py:221-225`: `azure_openai_endpoint` is stored unvalidated and connected to.
- `backend/app/services/tools/shopify_tools.py:35-42`: `shop_domain` becomes the request host.
- Effect: a tenant can make the backend call internal addresses, including a cloud metadata service.
- Outside pattern: OWASP SSRF Prevention Cheat Sheet, case 2: verify every resolved address is public, connect only to the validated address, disable redirects (https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html). Mealie ships this as a transport wrapper (`mealie-recipes/mealie`, `mealie/pkgs/safehttp/transport.py`).

### S4. Telnyx webhooks are not checked for age. CONFIRMED
- `backend/app/core/webhook_security.py`, `validate_telnyx_signature`: the timestamp is part of the signed string but is never compared with the clock.
- A captured signed inbound webhook can be replayed at any time. The reply contains a fresh stream token (`telephony.py:1139`), which admits the sender to the agent.
- Telnyx: "To prevent replay attacks, reject webhooks where `telnyx-timestamp` is more than 5 minutes old" (https://developers.telnyx.com/docs/messaging/messages/receiving-webhooks).

### S5. Live calls are cut on every deploy. CONFIRMED (config), PLAUSIBLE (runtime)
- `backend/app/core/config.py:232`: the app drains calls for 120 s.
- `backend/gunicorn.conf.py`: `graceful_timeout = 30`, so workers are killed at 30 s.
- `docker-compose.prod.yml`: no `stop_grace_period`, so Docker kills the container 10 s after SIGTERM.
- `gunicorn.conf.py`: `max_requests = 10000` recycles workers that may be holding calls.
- Outside pattern: LiveKit's self-hosting guide: "Voice AI apps might require a 10+ minute grace period to allow for conversations to finish" (https://docs.livekit.io/agents/ops/deployment/custom/).

### S6. The database pool can ask for eight times what Postgres allows. CONFIRMED
- `backend/app/db/session.py:13-14`: 50 + 50 connections per process.
- `backend/gunicorn.conf.py`: up to 8 workers. Peak demand 800 against the Postgres default of 100.

### S7. The backend image runs as root and the app ports bypass TLS. CONFIRMED
- `backend/Dockerfile:16-46`: no `USER`; base image by tag, not digest; `pip install uv` unpinned (`:7`). The frontend image does drop to a non-root user.
- `docker-compose.prod.yml:93-94, 122-123`: ports 8000 and 3000 are published on the host, so the API answers over plain HTTP beside the TLS proxy.
- Outside pattern: OWASP Docker Security Cheat Sheet rule 2, "Configuring the container to use an unprivileged user is the best way to prevent privilege escalation attacks" (https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html). Every LiveKit agent example image ends in `USER appuser` (`livekit/agents`, `examples/healthcare/Dockerfile`).

### S8. No backups, and migrations run unattended at every start. CONFIRMED
- `docker-compose.prod.yml:129-133`: one local volume each for Postgres and Redis; no backup job, no restore procedure in the repository.
- `backend/scripts/docker-entrypoint.sh`: `alembic upgrade head` on every boot, with no lock for more than one replica and no rollback step.

### S9. Where call content goes is not controllable or disclosed per tenant. CONFIRMED
- `backend/app/services/gpt_realtime.py:239, 271, 357`: the default path is OpenAI direct with `whisper-1` transcription. Azure is optional.
- `backend/app/core/config.py:183, 217`: with QA on, transcripts go to Anthropic on the platform's own key. `models/privacy_settings.py:40-55` tracks a DPA flag for four vendors and not this one.
- `backend/app/models/call_record.py:118`: transcripts are plain text in `call_records.transcript`.
- `enable_recording` is stored and shown but nothing records (`agents.py:155`; no writer of `recording_url`). The dashboard implies a feature that does not exist.

### S10. Operational endpoints are public. CONFIRMED
- `backend/app/main.py:203-205`: `/docs`, `/redoc` and `/openapi.json` are always on.
- `main.py:251-252`: `/metrics` is on by default with no auth and carries agent ids as labels (`monitoring/metrics.py`).
- `backend/app/api/health.py:150-165`: `/health/detailed` returns raw exception text.
- Outside pattern: disable docs outside development (`docs_url=None if settings.ENVIRONMENT == "production"`; `kevaldekivadiya2415/fastapi-genai-boilerplate`, `app/core/server.py`).

### S11. The owner id is an integer in some tables and a derived UUID in others. CONFIRMED
- `backend/app/core/auth.py:19-28`: the UUID is a hash of the integer, with no foreign key. `:62-82` reverses it by scanning every user.
- Integer owner: agents, workspaces, contacts, privacy settings. UUID owner: call records, phone numbers, campaigns, settings, integrations.
- Handlers that compare the wrong pair and so never work on Postgres: `telephony.py:752` (manual outbound call), `campaigns.py:436, 471` (create campaign), `realtime.py:785` (save dashboard transcript, always 403), and the four lines in B5. None has a test.
- These fail closed, but it is the root cause of B5 and a standing source of tenancy mistakes.

### S12. A tenant is one login. CONFIRMED
- No membership table; every object hangs off one user. `is_superuser` is set once (`main.py:103`) and read nowhere.
- A clinic's staff would share one password. No roles, no MFA, no SSO.

## Hygiene

| # | Finding | Where | State |
| --- | --- | --- | --- |
| H1 | Login token kept in `localStorage`; sent in the socket query string. Embed session token also travels in a URL | `frontend/src/hooks/use-auth.tsx:34, 101`; `realtime.py:105-125`; `embed.py:216` | CONFIRMED |
| H2 | No HSTS; CSP allows `localhost` connections; deprecated `X-XSS-Protection` | `middleware/security.py:21-42` | CONFIRMED |
| H3 | Sentry sends stack-frame local variables by default and traces at 100% | `main.py:122-126`; `config.py:166`. Sentry docs: `include_local_variables` default `True` (https://docs.sentry.io/platforms/python/configuration/options/) | PLAUSIBLE |
| H4 | Error bodies echo internal exception or vendor text | `tools.py:93`; `realtime.py:568, 578, 670, 723` | CONFIRMED |
| H5 | Embed transcript is unbounded client text stored as a genuine call record | `embed.py:656-660, 848` | CONFIRMED |
| H6 | Rate-limit key is the socket peer; behind a proxy all clients share one bucket | `core/limiter.py:5` | PLAUSIBLE |
| H7 | Email addresses and phone numbers in warning-level logs | `auth.py:177, 185`; `telephony.py:913` | CONFIRMED |
| H8 | `transfer_call` and `send_dtmf` report success and nothing performs them | `call_control_tools.py:126-167`; `telephony_ws.py:250, 758` handle `end_call` only | CONFIRMED |
| H9 | First-start admin creation races across workers | `main.py:92-115` | PLAUSIBLE |
| H10 | CI has dependency audit only: no static analysis, secret scan or image scan; actions pinned by tag | `.github/workflows/ci.yml` | CONFIRMED |
| H11 | Compose health check ignores `POSTGRES_USER`; Redis password on the command line | `docker-compose.prod.yml:19, 32, 36` | CONFIRMED |

## What holds up

- Carrier stream tokens: domain-separated key, bound to provider, agent and call, constant-time compare, single use with Redis fail-closed (`core/stream_token.py`, `core/stream_token_use.py`).
- Embed sessions: signed, site-bound, one socket per session, tools off by default, tool names checked against the enabled set.
- Twilio verification resolves the secret from the webhook's target, never from the sender's claimed account (`core/webhook_security.py`).
- `SECRET_KEY` placeholder refusal at import; unsigned webhooks need an explicit, loudly logged setting.
- Trace export refuses cleartext to a non-loopback host.
- CI runs the suite on Postgres; campaign dialling uses `SKIP LOCKED`.
- No owner-defined HTTP tool exists, so the classic "tool calls an arbitrary URL" hole is absent; S3 lists the three places a tenant URL does reach the server.

## What a UK healthcare buyer will ask

| Question | Answer today |
| --- | --- |
| Where is call audio and text processed? | OpenAI direct by default; region not selectable. Azure optional per workspace |
| Are our keys and tokens encrypted? | No (B1) |
| Can another customer see our data? | Yes, through four routes, given an agent id (B3) |
| Can you erase a patient's data on request? | Not by caller; account-level erase is broken (B5) |
| Is data deleted after the retention period? | No job runs (B5) |
| Who accessed or changed what? | No audit trail (B6) |
| Do staff have their own logins and MFA? | No (S12) |
| Are calls recorded, and are callers told? | Nothing is recorded; no caller notice mechanism (S9) |
| Backups and restore test? | None in the repository (S8) |
| Which sub-processors? | OpenAI, carrier, optionally Anthropic for QA; the last is not tracked (S9) |

## Not checked

- Runtime behaviour: nothing was run, so every "raises on Postgres" is from reading the types and the repository's own comment on the mirror case.
- The frontend beyond token storage; the embed widget script.
- Whether Alembic migrations match the models.
- The `caller/` package.
- The real proxy in front (Coolify/Traefik): header forwarding, TLS, whether it overrides the published ports.
- Dependency advisories (CI's `pip-audit` and `npm audit` cover these).
- Redis and Postgres network exposure on a real host.

## Method

- Fresh clone at `56702fa`; route table built by an AST walk over `backend/app/api/*.py`; each handler's query filters read.
- Outside grounding: OWASP API Security Top 10 2023, OWASP cheat sheets (Secrets Management, SSRF Prevention, Docker Security), Telnyx and Twilio webhook docs, Sentry Python options, LiveKit self-hosting guide; public code in `langgenius/dify`, `fastapi/full-stack-fastapi-template`, `livekit/agents`, `mealie-recipes/mealie`. No project, owner or client name was sent to any outside service.
