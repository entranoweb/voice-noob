# Synthiq Voice call-path audit (25 Nov 2026)

Read-only audit of `entranoweb/voice-noob` at `56702fa` (main, after #24). No code was changed and no call was placed. Every finding was traced in the code; none was reproduced on a live call.

## Answer

The call path would not survive a paying deployment as it stands. Authentication of the media socket is now sound, but the bridge behind it has seven faults that each break a real call or a real deployment, and none of them is a security issue:

1. The bridge does not stop when one side stops. A caller who hangs up leaves the model session, a database connection and the registry entry held until the 5-minute timer fires.
2. Every call is cut off at 5 minutes with no warning.
3. "Transfer to a human" and keypad tones are accepted by the agent and silently do nothing.
4. Inbound calls never reach a finished state in the database, so they have no duration and are never evaluated.
5. Campaign calls create no call record, so campaign contacts stay "calling" forever and the campaign stalls.
6. A deploy or a routine worker recycle drops every live call; the drain logic runs after the calls are already dead.
7. Errors from the model provider are ignored, which produces a silent call with healthy logs.

Counts: 7 block a paying deployment, 8 hurt under load, 6 are hygiene. Three are marked PLAUSIBLE (need a run); the rest are CONFIRMED by trace.

Already in flight elsewhere and not repeated here: Telnyx signature per workspace, shared rate limits, the `/token` instructions leak, `frame-ancestors`, compose environment passthrough, docs.

## Findings

| # | Finding | Where | Verdict | Severity |
|---|---|---|---|---|
| B1 | Bridge keeps running after one side ends | `backend/app/api/telephony_ws.py:815-823`, `1223-1231` | CONFIRMED | Blocks |
| B2 | Hard 5-minute cap on every call | `telephony_ws.py:822`, `1230` | CONFIRMED | Blocks |
| B3 | `transfer_call` and `send_dtmf` do nothing but report success | `backend/app/services/tools/call_control_tools.py:126-167`; `telephony_ws.py:250`, `758` | CONFIRMED | Blocks |
| B4 | Inbound calls never get a final status | `backend/app/api/telephony.py:571-575`; `telnyx_service.py:76-81` | CONFIRMED | Blocks |
| B5 | Campaign calls have no call record; contacts stick at "calling"; dial rate multiplies per worker | `backend/app/services/campaign_worker.py:324-377`, `136-158`; `telephony.py:978-1030`; `main.py:131-135`; `gunicorn.conf.py:16` | CONFIRMED | Blocks |
| B6 | Deploys and worker recycling drop live calls; drain runs too late | `backend/gunicorn.conf.py:19-24`; `backend/app/main.py:147-160` | CONFIRMED (code and upstream source; not run) | Blocks |
| B7 | Provider `error` events are ignored | `telephony_ws.py:698-809`, `1135-1217` | CONFIRMED | Blocks |
| L1 | Twilio path never clears playback on barge-in; neither path truncates the model's item | `telephony_ws.py:804-809` (Twilio), `1197-1210` (Telnyx) | CONFIRMED | Hurts |
| L2 | The agent's goodbye is cut off when it ends the call | `telephony_ws.py:798-801`, `828-831`, `1191-1195`, `1237-1240` | CONFIRMED | Hurts |
| L3 | Tools run inline in the event reader with no overall timeout | `telephony_ws.py:756`, `1165`; `gpt_realtime.py:508`; `services/tools/*.py` (`timeout=30.0`) | CONFIRMED | Hurts |
| L4 | One database connection is pinned for the whole call | `telephony_ws.py:466`, `840`; `backend/app/db/session.py:18-21` | CONFIRMED | Hurts |
| L5 | Blocking Twilio client inside async functions | `backend/app/services/telephony/twilio_service.py:62`, `95`, `110`, `164`, `195`, `223`, `260`, `342` | CONFIRMED | Hurts |
| L6 | Evaluation races the transcript and is skipped for good when it loses | `telephony.py:1022-1025`, `1237-1240`; `services/qa/evaluator.py:277-279` | CONFIRMED | Hurts |
| L7 | A repeated webhook creates a second call row, after which lookups raise | `models/call_record.py:58-63`; `migrations/versions/007_add_call_records.py:147`; `telephony.py:928-943`, `978-979` | CONFIRMED | Hurts |
| L8 | Azure path connects with a preview API version but sends the GA payload | `backend/app/services/gpt_realtime.py:221-225` | PLAUSIBLE | Hurts |
| H1 | Twilio path records no turns, no trace and no reason the call ended | `telephony_ws.py:462-603` against `836-1048` | CONFIRMED | Hygiene |
| H2 | Production log level drops every call lifecycle line | `main.py:57-59` | CONFIRMED | Hygiene |
| H3 | Model, transcription model and turn-detection values are hard-coded | `gpt_realtime.py:271`, `357-363` | CONFIRMED | Hygiene |
| H4 | Campaign dials before it commits; a crash in between re-dials the person | `campaign_worker.py:357-371`, `256` | PLAUSIBLE | Hygiene |
| H5 | Drain counts every worker's calls and stale entries | `services/call_registry.py:168-189`; `core/config.py:233` | CONFIRMED | Hygiene |
| H6 | Status callbacks applied in arrival order can move a call backwards | `telephony.py:1004` | PLAUSIBLE | Hygiene |

## Detail, with the outside pattern beside each

### B1. The bridge keeps running after one side ends

Both bridges run the two directions with `asyncio.gather(..., return_exceptions=True)` inside `asyncio.wait_for(..., timeout=300.0)`. `gather` returns only when both finish.

- Caller hangs up: the carrier loop breaks on `stop`. The model loop is `async for event in realtime_session.connection`, and an idle model session sends nothing, so it waits until the 300-second timer.
- Until then the call still holds its model session, its database connection (L4) and its registry entry. The transcript, turns and termination reason are written only after `gather` returns, so they land up to five minutes late, and are lost if the worker restarts in that window.
- Model socket drops: the model loop ends, the carrier loop keeps reading audio, and `send_audio` logs a full traceback for every 20 ms frame (`gpt_realtime.py:604-605`) while the caller hears silence until they hang up.

Outside pattern:
- Twilio's own sample closes the model socket when the carrier disconnects: `except WebSocketDisconnect: ... if openai_ws.state.name == 'OPEN': await openai_ws.close()` (twilio-samples/speech-assistant-openai-realtime-api-python, `main.py`, about lines 101-104). https://github.com/twilio-samples/speech-assistant-openai-realtime-api-python/blob/main/main.py
- LiveKit's realtime plugins wait on the first task to finish and cancel the rest: `done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)` (livekit/agents, `livekit-plugins-openai/.../realtime/realtime_model.py`, about line 1264). https://github.com/livekit/agents/blob/main/livekit-plugins/livekit-plugins-openai/livekit/plugins/openai/realtime/realtime_model.py

### B2. Hard 5-minute cap

`timeout=300.0` is commented "5 minute max call duration before forced cleanup". When it fires the handler returns, the socket closes and the carrier ends the call mid-sentence. The Twilio path records nothing about why; the Telnyx path records `MAX_DURATION`. A booking call that runs past five minutes is cut off.

Outside pattern: the provider's own ceiling is 60 minutes per session ("Realtime sessions have a maximum duration of 60 minutes", Microsoft Learn, realtime-audio). LiveKit makes the limit a setting (`max_session_duration`) and reconnects rather than hanging up. A cap should be per agent, announced to the caller before it applies, and recorded.

### B3. Transfer and keypad tools do nothing

`CallControlTools` returns `{"success": True, "action": "transfer_call", ...}` and `{"action": "send_dtmf", ...}` and says "the actual execution is handled by the realtime session". The bridges act only on `action == "end_call"`. So the agent tells the caller it is transferring them, the model is told the transfer succeeded, and the caller stays on the line with the agent. For a receptionist this is the emergency and "speak to a person" path.

Outside pattern: Pipecat's Twilio serializer ends or redirects the call through the carrier's REST API rather than relying on the socket (`auto_hang_up`, pipecat-ai/pipecat `src/pipecat/serializers/twilio.py`, about line 61). A transfer on Twilio is a REST update of the live call with new TwiML (`<Dial>`); on Telnyx it is the transfer command. Until that exists the two tools should not be offered to the model.

### B4. Inbound calls never get a final status

`_configure_webhook_for_provider` passes only `voice_url`. `configure_phone_number_webhook` sets a status callback only when one is given, so numbers bought through the product have none. The Telnyx outbound call payload carries no status callback either. Only Twilio outbound calls placed through the API set one (`twilio_service.py:60-69`).

Effect: an inbound call's record stays at `ringing` with no `answered_at`, `ended_at` or duration, and the evaluation that hangs off the `completed` status never starts. A number configured by hand in the carrier's console could work around it; nothing in the product does.

Outside pattern: Twilio documents `status_callback` on the number as the way to learn that an inbound call ended. The bridge also knows the call ended and could write the end state itself rather than depend on a second webhook.

### B5. Campaign calls

`CampaignWorker._initiate_call` dials and marks the contact `CALLING` but creates no `CallRecord`. The status webhooks update a campaign contact only inside `if call_record:`. So no campaign call is ever marked finished, `active_calls` only grows, and the campaign stops dialling once `max_concurrent_calls` contacts are stuck. Transcripts for these calls have no row to attach to.

Separately, the worker is started in `lifespan`, which runs once per gunicorn worker process, up to eight. `skip_locked` stops two processes taking the same contact, but each process applies `calls_per_minute` and `max_concurrent_calls` on its own, so the real dial rate is up to eight times the configured one.

Outside pattern: one scheduler, held by a lock or run as its own process; mark the row and commit before dialling (H4).

### B6. Deploys and recycling drop live calls

`gunicorn.conf.py` sets `max_requests = 10000` with `graceful_timeout = 30`. Health checks and webhooks count as requests, so each worker is recycled regularly, and on shutdown uvicorn closes every open websocket at once:

```
def shutdown(self) -> None:
    self.ws_server.closing = True
    if self.handshake_completed_event.is_set():
        self.fail_connection(1012)
```
(Kludex/uvicorn, `uvicorn/protocols/websockets/websockets_impl.py`, about line 150. https://github.com/Kludex/uvicorn/blob/main/uvicorn/protocols/websockets/websockets_impl.py)

The application's own drain (`set_shutting_down(True)` then `wait_for_calls_to_drain`) is in the shutdown half of `lifespan`, which runs after the server has already closed those connections. `is_shutting_down()` is therefore never true while a call could still be refused, and the drain waits on calls that are gone.

Outside evidence that this bites: a public load test traced about 1,500 unexpected websocket closes in ten minutes to `--max-requests 10000` and raised it twentyfold (https://github.com/carlozrodri/kuulis/pull/33). Pattern: no request-count recycling on workers that hold calls; drain on SIGTERM before the server stops (stop taking new calls, wait for live ones, then exit), with the orchestrator's stop grace period set longer than the longest call.

### B7. Provider errors are ignored

Neither bridge loop has a branch for `error` events. The file's own comment in `gpt_realtime.py:333-344` describes the result: a rejected `session.update` means `session.updated` never arrives, the greeting never fires, and the call is silent with healthy logs. The same holds for a rate limit or a rejected tool schema. An unused method (`process_realtime_events`, `gpt_realtime.py:461-462`) does log errors; the live path does not.

Outside pattern: the Microsoft sample loop breaks on `message.type === "error"`; Pipecat and LiveKit raise a session error and end the call with a spoken fallback. At minimum: log at error level, mark the call failed, play a fallback line and hang up.

### L1. Barge-in

Telnyx: on `speech_started` while the agent is speaking the bridge sends `{"event": "clear"}`. Twilio: the same event is only logged, so audio already buffered at Twilio keeps playing over the caller. Neither path sends `conversation.item.truncate`, so the model's context keeps the full sentence the caller never heard.

Outside pattern (Twilio sample, about lines 136-172): on `input_audio_buffer.speech_started`, send `conversation.item.truncate` with `audio_end_ms` computed from the carrier's media timestamps, then `{"event": "clear", "streamSid": ...}`, then empty the mark queue. Microsoft's docs give the reason: "The server produces audio faster than real-time. The client can send a conversation.item.truncate event to truncate the audio before it's played."

### L2. The goodbye is cut off

After `end_call`, the bridge waits for `response.done` and closes the socket. `response.done` means the model finished generating, not that the carrier finished playing; generation runs faster than real time.

Outside pattern: send a `mark` after the last audio frame and close only when the carrier echoes it back (the Twilio sample's `send_mark` and mark queue, about lines 174-182).

### L3. Tools run inline

The tool call is awaited inside the `async for event in connection` loop. While it runs, no provider events are read: no barge-in, no error, no transcript. Each outbound HTTP client allows 30 seconds and nothing bounds the tool as a whole. The caller hears silence for as long as the slowest back-end takes, and a caller who hangs up mid-tool is not noticed by this loop.

Outside pattern: Pipecat runs function calls as separate tasks with `function_call_timeout_secs` and a per-tool `timeout_secs`; a call that overruns "is now cancelled rather than left to run" (pipecat-ai/pipecat, `src/pipecat/services/llm_service.py`, about lines 186-316, and CHANGELOG).

### L4. A database connection per call

Both socket endpoints take `db: AsyncSession = Depends(get_db)`. The session runs selects at the start and is not committed or closed until the call ends, so each live call holds one pooled connection "idle in transaction" for its whole length, plus up to five more minutes under B1. The pool is 50 plus 50 overflow per process with `pool_timeout=5`, and there can be eight processes: up to 800 connections against a database that typically allows about 100.

Outside pattern: open a short session per database operation (`async with AsyncSessionLocal()`), never one per connection lifetime.

### L5. Blocking Twilio client

`TwilioService` methods are `async def` but call the synchronous `twilio.rest.Client` directly. Each call blocks the event loop for the length of an HTTPS request, which stalls audio for every live call on that worker. The campaign worker uses the same client in a loop.

Outside pattern: twilio-python's README shows `AsyncTwilioHttpClient()` and `create_async(...)` (https://github.com/twilio/twilio-python/blob/main/README.md, about line 234), or `asyncio.to_thread`.

### L6. Evaluation races the transcript

The status webhook queues the evaluation as soon as the carrier reports `completed`. The transcript is written by the bridge when the socket handler finishes, which under B1 can be minutes later. The evaluator returns `None` on `if not call_record.transcript` and nothing retries.

Pattern: trigger the evaluation from the place that writes the transcript, or retry with a delay.

### L7. Repeated webhooks

`provider_call_id` has an ordinary index, not a unique one. The voice webhooks insert a row on every delivery. Telnyx documents at-least-once delivery with retries ("handlers should be idempotent", https://developers.telnyx.com/docs/voice/programmable-voice/voice-api-webhooks); Twilio retries once on connection failure by default. After a duplicate, every `scalar_one_or_none()` lookup for that call raises, so the status webhook returns 500 (which invites more retries) and the transcript save fails.

Pattern: a unique constraint on provider plus call id, and insert-or-fetch in the webhook.

### L8. Azure path (PLAUSIBLE)

`AsyncAzureOpenAI(..., api_version="2024-10-01-preview")` is used with `client.realtime.connect` and the GA session payload and GA event names. Microsoft's current guidance: "For the Realtime API, use the GA endpoint with `/openai/v1` in the URL. Don't use date-based API versions or the api-version query parameter." (https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/realtime-audio). If the preview protocol answers, the event names differ and the result is the silent call described in B7. Needs one call against an Azure deployment to settle.

### Hygiene

- **H1.** The Twilio endpoint was not given the turn recorder, trace emitter or termination reason that the Telnyx endpoint has. The caller rig's phone leg is on Twilio, so its calls produce none of that telemetry.
- **H2.** Outside debug mode the logger is filtered at WARNING, so `call_registered`, `twilio_stream_started`, `transcript_saved` and the rest never appear. An incident on a live call would have almost nothing to read.
- **H3.** The model is pinned to `gpt-realtime-2025-08-28`, transcription to `whisper-1`, and server VAD to 200 ms of silence with no per-agent setting. 200 ms matches the provider default, but callers reading a postcode pause longer than that; `semantic_vad` exists for this. Worth a setting and a measured comparison with the caller rig.
- **H4.** The campaign worker dials, then commits. A crash between the two leaves the contact pending and it is dialled again.
- **H5.** `wait_for_calls_to_drain` counts every key in Redis, so one process waits on other processes' calls and on entries left by a crash (TTL 30 minutes).
- **H6.** Status callbacks are applied in arrival order; a late `in-progress` after `completed` would move the record backwards. Twilio and Telnyx both say order is not guaranteed.

## Not checked

- Nothing was run: no carrier call, no model session, no load.
- Whether an idle model session sends any event that would end B1's wait sooner. The trace says it does not; a run would settle it.
- Telnyx TeXML wire behaviour (stream start frame, `clear`, end of stream).
- The browser and embed paths, the QA harness, and the `caller/` package: out of scope for this lens.
- Twilio's `SequenceNumber` field for ordering voice status callbacks: not confirmed in a primary source during this audit.
