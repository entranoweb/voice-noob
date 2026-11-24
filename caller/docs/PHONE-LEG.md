# The phone leg

The phone leg places a real telephone call through Twilio and carries the
caller's clips over it. It exists because the socket leg skips the telephone
codec, so it cannot reproduce mishearing that only happens on a phone line.

**No call has been placed with this code.** Everything below the carrier is
covered by tests against a stand-in for Twilio. Anything that involves a real
carrier is unverified until the first call, and is listed at the end.

## How it works

1. `synthiq-caller-phone` starts one small server. It takes Twilio's media
   websocket at `/media/<token>` and Twilio's call-status requests at
   `/status/<token>`, both on the same port.
2. It asks Twilio to dial `--to` from `--from` with inline TwiML:
   `<Response><Connect><Stream url="wss://…/media/<token>"><Parameter name="token" …/></Stream></Connect></Response>`.
3. Twilio connects the websocket and sends `connected`, `start`, then `media`
   frames (G.711 mu-law, 8000 Hz, base64). The agent's audio becomes
   `AgentAudio` events for the director.
4. The caller's clips are encoded to mu-law and sent as `media` messages, one
   20 ms frame every 20 ms. Frames of pure silence are not sent.
5. Hanging up ends the call through Twilio's REST API.

## What a phone line does not give the caller

| Missing | What the leg does |
| --- | --- |
| The agent's words | A transcriber listens to the agent's side and the leg emits the text as `AgentText`. `--transcriber deepgram` (default) streams the mu-law audio to Deepgram's live endpoint; `scripted` emits lines from a file in order; `none` emits nothing |
| What the agent's recogniser heard | Nothing. `Heard` is never emitted on this leg. Read it afterwards from the agent engine's trace |
| A way to press a key | Twilio's stream accepts only `media`, `mark` and `clear` from our side. `send_dtmf` generates the two keypad tones as audio and plays them in the call |

## First real call

You need:

- A Twilio account, with `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in the
  environment. `TWILIO_API_KEY_SID` and `TWILIO_API_KEY_SECRET` are optional and
  are used for the REST calls when set; the auth token is still needed, because
  it is what Twilio signs its status requests with.
- A Twilio number (or verified caller ID) to dial from, in E.164 form.
- Permission in the Twilio console to dial the destination country.
- An address Twilio can reach, with TLS, that forwards both plain HTTPS and
  websocket upgrades to the port this command listens on.
- For scenario-driven calls, `DEEPGRAM_API_KEY`, or a file of agent lines.
- ffmpeg on the path if the clip bank is not already 8000 Hz.

- A list of the numbers this caller may dial (next section).

### Only listed numbers are dialled

A real call is placed only to a number the operator has listed beforehand. A
mistyped digit reaches a stranger; a copied command can reach a live line. The
list is a file with one number per line (`#` starts a comment), or a
comma-separated value, given with `--allowed-numbers`; the
`SYNTHIQ_CALLER_ALLOWED_NUMBERS` variable adds to it. The match is on exact
digits. With no list, nothing is dialled: not by `batch`, not by the single-call
command, and not by the leg itself.

### Check first, without a call

```
export TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... DEEPGRAM_API_KEY=... ELEVENLABS_API_KEY=...
synthiq-caller preflight --leg phone \
  --public-url https://caller.example.com \
  --from +44XXXXXXXXXX --to +44XXXXXXXXXX \
  --allowed-numbers allowed.txt --port 8765
```

It places no call. It checks that:

| Check | How |
| --- | --- |
| The credentials and keys are set | Reads the environment; prints names, never values |
| Both numbers are written in full | `+<country><number>` |
| The number to dial is on the list | Exact digits |
| The caller number is on the account | One read of the account's numbers from Twilio. A caller ID that was only verified is not looked up and fails this check |
| The public address reaches this process | Starts the listening server and asks the public address for a probe only this process can answer |
| The public address carries a media stream | Opens a websocket through it to the same probe. This is what catches a proxy that does not forward upgrades |

One more line is a warning, not a failure: whether the caller number's own
voice webhook points at this caller. An outbound call does not use that
webhook (the stream address travels in the call's TwiML). It matters only if
something rings the caller's number back.

The exit code is 1 when any check failed.

### One call

```
synthiq-caller-phone \
  --public-url https://caller.example.com \
  --from +44XXXXXXXXXX --to +44XXXXXXXXXX \
  --allowed-numbers allowed.txt \
  --scenario scenarios/new-patient-date-of-birth.json \
  --clips <clip bank directory> \
  --out runs/ --port 8765
```

Rehearse the same command with `--dry-run` first. It places no call, needs no
credentials, and prints the TwiML and the audio that would be sent. This
command writes `phone-run-NNN.json`, which is not graded; use `batch` for a
graded call.

## A batch of calls, graded

```
synthiq-caller batch scenarios/sophie --leg phone \
  --public-url https://caller.example.com \
  --from +44XXXXXXXXXX --to +44XXXXXXXXXX \
  --allowed-numbers allowed.txt \
  --agent <agent id> \
  --clips <clip bank directory> --out runs/phone \
  --only 11-similar-name --repeats 1 --fail-under 95
synthiq-caller report runs/phone
```

This is the same batch as over a socket: the same scenario files, the mutation
guard (`--allow-mutating`), the name budget, reset, read-back and chains, and
run files of the same shape, so `report`, `regrade`, `baseline` and `compare`
read them unchanged. What differs:

- **One server for the whole batch.** It listens once; each call registers its
  own token and is handed only the requests that carry it.
- **One call at a time.** `--max-parallel` above 1 is refused.
- **Each run says how it travelled.** `leg` is `phone`, and `phone` holds the
  numbers, the carrier's log and the times the call was dialled, answered and
  ended. The report prints calls per line.
- **`record.call_ref` is the carrier's call id**; `trace.call_ref` is the agent
  platform's id for the same call.

`--fail-under <percent>` on `batch` and `report` makes the exit code 1 when
fewer than that share of the calls placed passed. A call that could not be
graded counts against it, so a batch cannot clear the gate by losing its
traces. With no calls placed the exit code is 1.

A call that is not answered still stops the batch with an error, as it does on
the socket leg.

### Finding the call's trace

The caller holds the carrier's id for its own outbound leg. The agent's
platform files the call under another id, for the inbound leg it answered, so
the trace has to be looked for.

For ElevenLabs the lookup lists conversations that started within
`--trace-window` seconds (default 120) either side of the call, limited to
`--agent` when given, and keeps those whose `metadata.phone_call` shows an
inbound call to the dialled number (`agent_number`) from the caller's number
(`external_number`). `--trace-ignore-caller-number` drops the last condition,
for a line that withholds the caller's number.

Exactly one conversation must match. None, or more than one, and the call is
**ungraded: trace not found**. It is never passed, never failed, and never
matched to the nearest call. The report lists it with that reason. When the
platform lists the call later:

```
synthiq-caller regrade runs/phone --scenarios scenarios/sophie --reread --leg phone --agent <agent id>
```

`--no-trace` places calls without looking; every one is then ungraded.

Other platforms are not built. The expected equivalents are below; they are
written from memory of each platform's call-listing API, so check every field
name against the current reference before building one:

| Platform | Equivalent lookup |
| --- | --- |
| Retell | List calls filtered by `to_number`, `from_number` and a `start_timestamp` range, then read the one call with the Retell trace adapter |
| Vapi | List calls created inside the time range for the phone number's id, match `customer.number` to the caller's number, then read the one call with the Vapi trace adapter |

Each is one class beside `ElevenLabsNumberTimeFinder` in `traces/lookup.py`.

### Each side of the call, as its own file

Every call in a phone batch writes two files under the run directory, and the
run file points at them under `audio`:

- `audio/<carrier call id>.agent.wav`: the audio that arrived from the far end,
  placed by the carrier's own timestamp on each frame.
- `audio/<carrier call id>.caller.wav`: the audio this process released, placed
  by when it was released.

Both are 8000 Hz, 16-bit, mono, the same length, and start when the media
stream started, so a time in one is the same time in the other. Neither is
what the other side heard; the network and the carrier sit in between. For
that, use the carrier's own two-channel recording. `--no-audio` keeps neither.

### Check in this order

| # | Check | If it fails |
| --- | --- | --- |
| 1 | The command prints a call id beginning `CA` | Twilio refused the request. The error text names the cause: unverified number, country not enabled, bad TwiML |
| 2 | `statuses` in the output include `ringing` and `in-progress` or `answered` | Status requests are not reaching `/status/…`, or their signature failed. `phone.rejected` in the run file says which |
| 3 | `phone.stream_sid` is set | The websocket never started. Suspect the public address or a proxy that does not forward upgrades |
| 4 | `frames sent` is above zero and the agent reacts to the opener | Audio is not reaching the far end. Listen to the recording |
| 5 | The run ends `caller hung up: scenario end` | The scenario never matched an agent line. Look at the `agent` entries in the timeline: they are what the transcriber produced |
| 6 | A dual-channel recording is listed under `recordings` | Recording was not started, or is still being processed |
| 7 | The agent engine's trace shows the values the clips carried | This is the point of the leg: compare said with stored |

Cost per minute: not stated here. No figure has been checked against Twilio's
or Deepgram's current price lists.

## Direct, not Pipecat (for now)

The roadmap says the caller should become a Pipecat bot. This leg talks to
Twilio's media stream directly instead, for three reasons.

- The stream protocol is six message types. The whole leg is one module with no
  new dependency, against a `Leg` interface the director already uses.
- Pipecat's Twilio serializer is built to sit inside a Pipecat pipeline behind
  its FastAPI websocket transport. Using it here would mean running the director
  inside a pipeline, or a pipeline beside the director, to gain a codec this
  package already has.
- Its documentation covers keypad digits arriving, not being sent.

What Pipecat would add later, and when it becomes worth it:

- Smarter end-of-turn detection than an energy threshold.
- Recording each side as its own track inside the process.
- A SIP transport, so the caller can use any carrier and keep media in the UK.

When one of those is needed, the right move is a second leg (`legs/pipecat.py`)
behind the same `Leg` interface, not a rewrite of this one.

## Answer timing

On this leg the agent's words come from a transcriber, so they arrive after the agent has said them. The turn tracker used to treat the arriving text as the start of a line and wait a fixed 2.5 seconds for audio that had already played, which made every answer about three seconds late: long enough to trip an agent's "no answer" handling.

The leg now declares `text_follows_audio`. With it, agent speech heard since the caller's last answer is taken as the line the text belongs to, and the caller answers once that speech has been quiet for the usual moment. Speech from before the caller's last answer is never counted, so text that does arrive ahead of its audio is still waited for.

This is tested against the scripted leg only. On a real line, measure the gap between the end of the agent's speech and the start of the caller's answer in the first calls before using any phone result as evidence. Two things remain: the transcriber's own delay is still in every answer, and a line the transcriber returns without its question mark cannot be recognised as an unanswered question (see `OUTCOMES-AND-GATE.md`).

## Not verified

Nothing here has touched a carrier. Specifically:

- That Twilio accepts the inline TwiML and connects the stream.
- That the token arrives in the `start` message's `customParameters`.
- That Twilio's status requests, sent by GET, validate against the signature as
  computed here (HMAC-SHA1 over the full URL with its query string, keyed by the
  auth token, base64). Twilio's guidance is to use its own library for this; the
  check is implemented from the published description and tested only against
  itself.
- Whether the websocket handshake carries `X-Twilio-Signature`. The leg records
  whether it saw one (`phone.handshake_signature_seen`) and does not rely on it;
  the path token and the `start` token are what admit a stream.
- That in-band keypad tones are recognised by the far end after the carrier's
  path. They decode correctly after this package's own mu-law round trip.
- That a dual-channel recording is produced for a call whose only verb is
  `<Connect><Stream>`, and which channel carries which side.
- That frames paced by this process sound right on a real handset: no clipping,
  gaps or drift over a call of several minutes.
- Deepgram live transcription of real agent audio. The client is tested against
  a fake socket built from the documented message shapes. `language=en-GB` is
  assumed to be accepted; the reference page read on 24 Nov 2026 did not list it.
- Calling a UK number from a UK caller ID, and anything about answering-machine
  detection, spam labelling or rate limits on repeated calls.
- The trace lookup against a real account: that an inbound phone conversation
  is listed inside the time window, and that `metadata.phone_call.agent_number`
  and `external_number` hold the two numbers in a form the comparison accepts.
  Field names are from the API reference read on 25 November 2026.
- That the carrier stamps each inbound frame in milliseconds from the start of
  the stream. The agent's recording is placed by that stamp.
- The preflight against a real account and a real public address. Its probe has
  only been asked for over localhost.
- Several calls in a row through one listening server on a real line.

## Sources

Read on 24 November 2026.

- Twilio media stream messages: https://www.twilio.com/docs/voice/media-streams/websocket-messages
- Twilio call resource (create, update, recordings): https://www.twilio.com/docs/voice/api/call-resource
- Twilio request signatures: https://www.twilio.com/docs/usage/webhooks/webhooks-security
- Deepgram live transcription: https://developers.deepgram.com/reference/speech-to-text/listen-streaming
- ElevenLabs conversation list and detail (read 25 November 2026): https://elevenlabs.io/docs/api-reference/conversations/list and https://elevenlabs.io/docs/api-reference/conversations/get
- Pipecat Twilio serializer: https://docs.pipecat.ai/server/services/serializers/twilio
