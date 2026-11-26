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

- `Agent.phone_number_id` — written by `CreateAgentRequest` /
  `UpdateAgentRequest`. The **agent create and edit pages** send this.
- `PhoneNumber.assigned_agent_id` — written by
  `PUT /api/v1/phone-numbers/{id}`. The **phone-numbers page** sends this, and
  it sets no column on the agent.

Two screens offer the user the same choice and they write different tables, and
only the first used to route. Neither is enough on its own now. The rule is one
function, `resolve_number_route` in `app/services/telephony/number_routing.py`
(`get_agent_by_phone_number` wraps it), and a call routes only when ALL hold:

1. a `PhoneNumber` record exists for the number, is not released, and is the
   only live record for it;
2. its status is `active` (pending and suspended route nowhere);
3. it has a `workspace_id` (the workspace whose carrier account proved it, and
   whose key signs the webhook);
4. the agent is the owner's and has an `agent_workspaces` row for THAT workspace.

Among agents that pass 4: `assigned_agent_id` first, then agents naming the
number in `phone_number_id`, oldest first. Anything else routes nowhere and logs
`inbound_number_routes_nowhere` with a reason, never the number. The webhook
verifies the signature with the key of the number's workspace, not the agent's
first workspace. An agent in no workspace, a number with no record (legacy
rows), or a workspace deleted from under a number therefore silently stop
routing; `scripts/report_unverified_numbers.py` lists who.

The write paths (phone numbers, agents, workspace links, unlink, workspace
delete) refuse the changes that would break this with a 409/422, but they are
only a courtesy: the read path is the invariant. Do not add a route by patching
a write path.

How that stayed hidden is worth copying down. An OpenAPI dump was searched for
schemas named `AgentCreate` and `AgentUpdate`, which do not exist — the models
are `CreateAgentRequest` and `UpdateAgentRequest` — and the empty result was
read as "the field is not exposed anywhere". An empty answer is only evidence
once you have checked you asked the right question.

If you add a third way to associate a number with an agent, `resolve_number_route` is
where it has to be taught. `check_agent` in the preflight calls it, so it follows;
it drifted once while it had its own copy of the rule.

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

## 4. Signature verification fails closed

With `TELNYX_PUBLIC_KEY` unset, every webhook is refused 403. That used to
depend on `DEBUG` — an unset key accepted every webhook **unverified** when
`DEBUG` was true — and it no longer does. The only way to accept an unsigned
webhook is `ALLOW_UNSIGNED_WEBHOOKS=true`, which is off by default, logs a
critical line at startup and on every webhook it lets through, and fails the
preflight. Set the key; leave the override alone.

The Twilio verifier follows the same rule, and checks the signature against
the public URL (`PUBLIC_URL` + path), because that is the URL Twilio signed.

## 4a. The media stream needs a token, and the two carriers carry it differently

`/ws/telephony/{provider}/{agent_id}` admits nobody without the stream token
the answering webhook minted (`app/core/stream_token.py`): HMAC-SHA256 over
provider, agent id, call id and expiry, keyed from `SECRET_KEY`, valid for two
minutes. A refusal comes before 4003 and 4004 are ever reached, so an
unauthenticated caller cannot even learn whether an agent exists. What the
caller sees differs: the Telnyx check runs before the socket is accepted, so
the refusal is an **HTTP 403** on the upgrade (the code 4401 exists only in the
server's log and close call, never on the wire); Twilio's runs after accept and
is a real close frame, **4401**.

- **Telnyx**: the token is a `token=` query parameter on the `<Stream>` URL,
  beside `call_id`, and is checked before the socket is accepted. It is bound
  to that `call_id`, so editing the `call_id` in the URL invalidates it.
- **Twilio**: Twilio strips query strings from a `<Stream>` URL, so the token
  is a `<Parameter name="token">` and arrives in `start.customParameters`. The
  socket is accepted, then the first start frame must carry a valid token
  bound to that frame's `callSid`, within ten seconds, or it is closed.

Two consequences worth knowing before they cost an afternoon. A deployment
whose instances do not share one `SECRET_KEY` refuses its own calls, because
the webhook and the socket can land on different instances. And if a carrier
ever stops passing the query string through, every Telnyx call closes 4401 —
loudly (HTTP 403 on every upgrade, `telnyx_stream_token_rejected
reason=missing` in the log), not silently. That the Telnyx TeXML `<Stream>` preserves the query string has
still only been exercised by the rehearsal, never by the carrier.

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
