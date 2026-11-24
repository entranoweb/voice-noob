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

Then:

```
export TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... DEEPGRAM_API_KEY=...
synthiq-caller-phone \
  --public-url https://caller.example.com \
  --from +44XXXXXXXXXX --to +44XXXXXXXXXX \
  --scenario scenarios/new-patient-date-of-birth.json \
  --clips <clip bank directory> \
  --out runs/ --port 8765
```

Rehearse the same command with `--dry-run` first. It places no call, needs no
credentials, and prints the TwiML and the audio that would be sent.

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

## Sources

Read on 24 November 2026.

- Twilio media stream messages: https://www.twilio.com/docs/voice/media-streams/websocket-messages
- Twilio call resource (create, update, recordings): https://www.twilio.com/docs/voice/api/call-resource
- Twilio request signatures: https://www.twilio.com/docs/usage/webhooks/webhooks-security
- Deepgram live transcription: https://developers.deepgram.com/reference/speech-to-text/listen-streaming
- Pipecat Twilio serializer: https://docs.pipecat.ai/server/services/serializers/twilio
