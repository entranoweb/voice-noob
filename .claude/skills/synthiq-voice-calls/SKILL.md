---
name: synthiq-voice-calls
description: >-
  How a phone call actually reaches an agent in this repository, and the six
  ways it silently does not. Use this skill whenever you touch the inbound or
  outbound call path — telephony.py, telephony_ws.py, gpt_realtime.py,
  telnyx_service.py, docker-compose.prod.yml, DEPLOYMENT.md — or whenever the
  work involves Telnyx webhooks, TeXML answer documents, media streams,
  PUBLIC_URL, phone number assignment, the OpenAI Realtime session, or a call
  that connects but has no audio. Also use it before telling anyone a
  deployment is ready to take a call, and when a call "works" everywhere except
  the caller's ear. Every trap below was found by a call failing, not by
  reading, so reach for this before reasoning from the code alone.
metadata:
  author: synthiq
  project: synthiq-voice
---

# The inbound call path

A call reaching an agent crosses six systems, and **every failure between them
is silent**. The webhook returns 200, the document parses, the row lands, the
logs read healthy, and the caller hears nothing. That is the defining property
of this codebase's bugs: nothing throws, so nothing tells you.

Work accordingly. A change to this path is not verified by tests passing or by
the code reading correctly — only by a call, or by the rehearsal in
`references/rehearsal.md`, which reproduces one without a carrier.

## The path

```
Telnyx  ──POST──►  /webhooks/telnyx/voice      signature verified, agent resolved
                          │                     call_records row written
                          ▼
                   TeXML answer document        <Connect><Stream url="wss://…">
                          │
Telnyx  ──WSS───►  /ws/telephony/telnyx/{id}    agent loaded, workspace key read
                          │
                          ▼
                   OpenAI Realtime (GA)         session.update → session.updated
                          │
                          ▼
                   audio, turns, metrics
```

Each arrow is a place a call dies quietly. What follows is what is actually
true at each one.

## 1. Which agent a number reaches

Two columns record this and they are not the same:

- `PhoneNumber.assigned_agent_id` — what the dashboard and
  `PUT /api/v1/phone-numbers/{id}` write. This is the normal route.
- `Agent.phone_number_id` — a string on the agent that **no API can set**.
  Neither `AgentCreate` nor `AgentUpdate` exposes it, so only direct SQL
  populates it. Legacy rows use it.

`get_agent_by_phone_number` honours both, and released or suspended numbers
deliberately route nowhere. Before that was true, assigning a number exactly as
the product intends left the caller hearing *"no agent is configured for this
number"* while the dashboard showed the agent assigned.

If you add a third way to associate a number with an agent, that function is
where it has to be taught, and `check_agent` in the preflight has to learn it
too — it drifted once and reported a working deployment as broken.

An inactive agent closes the media stream with **4003**; an unknown agent id
closes it with **4004**.

## 2. The host in the stream URL

`build_telnyx_stream_url` takes its origin from `PUBLIC_URL`, falling back to
`request.base_url`. Set `PUBLIC_URL` in any deployment. `request.base_url` is
assembled from the request line and the Host header, and uvicorn ignores
forwarded headers unless `--forwarded-allow-ips` trusts the proxy — which on a
PaaS it does not by default. The carrier is then handed
`wss://internal:8000/...` and the stream connects to nothing.

**The scheme is never the problem.** The builder maps both `http` and `https`
to `wss`, so any check of the form `url.startswith("wss://")` passes no matter
which host is in it. An earlier runbook prescribed exactly that check, and a
test asserted exactly that. Assert the host.

## 3. TeXML, not Call Control

The number must be attached to a **TeXML application**. Call Control posts JSON
and expects API commands back; it ignores the document the webhook returns, so
the call goes nowhere. `parse_telnyx_webhook` accepts both wire formats, which
means a misconfigured number looks fine in the logs.

In the answer document, `bidirectionalMode="rtp"` is load-bearing: Telnyx
defaults it to `mp3`, and on the default the µ-law this bridge sends back is
read as an MP3 stream — the caller hears silence while the logs show audio
being written. `bidirectionalCodec="PCMU"` and
`bidirectionalSamplingRate="8000"` match the PSTN and the Realtime session.

## 4. Signature verification depends on DEBUG

With `TELNYX_PUBLIC_KEY` unset, `validate_telnyx_signature` returns
`settings.DEBUG`. So an unset key means every webhook is rejected 403 when
`DEBUG` is false, and every webhook is accepted **unverified** when it is true.
Both are wrong and neither announces itself. Set the key; keep `DEBUG` false.

## 5. The OpenAI key is per workspace

The bridge reads `user_settings.openai_api_key` for the agent's workspace via
`get_user_api_keys`, with no fallback to user-level settings and none to the
environment. `OPENAI_API_KEY` in the environment is read by other things and
will not make a call work. A workspace without a key raises *"API key not
configured for this workspace"* the moment the caller connects.

Check the workspace key, never the environment one, when reporting readiness.

## 6. The Realtime session must be the GA shape

The bridge connects on the GA namespace, and GA is not the beta interface:
audio settings live under `audio.input` / `audio.output`, `modalities` became
`output_modalities`, `type` is required, and `temperature` is gone. The full
accepted set is whatever `RealtimeSessionCreateRequest.model_fields` says.

Sending the beta shape raises nothing locally — `session.update` casts and
`send` transforms, neither validates — so the payload reaches OpenAI and is
rejected there as an asynchronous `error` event nothing awaits. The session
keeps its defaults (PCM16 at 24 kHz against 8 kHz µ-law), and `session.updated`
never arrives, so the greeting that waits on it never fires.

`_assert_ga_session` checks the payload before it is sent, including its keys
against the model's fields, because the model allows extras and validation
alone would pass every beta field. Keep new session code going through it.

## Before saying a deployment can take a call

```bash
make preflight ARGS="--url https://api.synthiqvoice.com"
```

Exit 0 or do not dial. It checks credentials, `PUBLIC_URL`, a single migration
head with the database at it, Redis, a reachable agent, that agent's workspace
key, the public host, and then opens a real websocket to the stream URL,
expecting 4004 from a random agent id — which proves TLS, the proxy's upgrade
handling, routing and the database session in one connection, without placing a
call or writing a row.

A `warn` is a judgement call. A `FAIL` is not.

## Environment facts that waste an afternoon

- **mypy**: `uv sync --all-extras --frozen` first. Without the extras it reports
  ~30 redis-py typing errors that are not real, because the stubs live there.
- **Tests**: without Postgres the suite falls back to sqlite and 16 tests fail
  on naive-vs-aware datetimes and savepoint rollback. They are not real either;
  against Postgres the suite is green. Set `TEST_DATABASE_URL` before believing
  a failure.
- **docker-compose.prod.yml** passes a *fixed list* of variables into the
  backend. A variable set in the deployment platform that the compose file does
  not name never reaches the app. `PUBLIC_URL` and `TELNYX_PUBLIC_KEY` are
  declared required there so a bad deploy fails naming the variable.

## How to prove a change to this path works

Reading the code is not enough — every trap above survived review. Reproduce a
call. **`references/rehearsal.md`** brings up Postgres, Redis and the app and
walks it end to end in about five minutes;
**`scripts/signed_webhook.py`** posts a genuine ed25519-signed TeXML webhook,
which is the only way to reach this endpoint at all — it verifies a signature
over `{timestamp}|{body}` before it does anything, so curl gets a 403 and tells
you nothing.

When you write a test for a fix here, revert the fix and confirm the test fails.
Two of this project's regressions were "covered" by assertions that held both
before and after.

State what you verified and how. If you could not verify something — the OpenAI
leg needs a real key, the carrier needs a real number — say so plainly rather
than implying the whole path was exercised.
