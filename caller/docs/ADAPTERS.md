# Trace adapters

A trace adapter reads back what an agent did during a call, in the platform's own
format, and turns it into the one shape the grader uses (`traces/base.py`):

- `tool_calls`: each tool the agent ran, with its arguments, its result, whether
  it succeeded, the flow step that ran it and the time into the call.
- `step_path`: the steps of the agent's flow, in the order they were entered.
- `termination`: why the call ended, in the platform's words.

There is one adapter per platform, each in its own module under
`src/synthiq_caller/traces/`.

## State of each adapter

| Platform | Module | Source | Run against a real account |
| --- | --- | --- | --- |
| ElevenLabs Agents | `elevenlabs.py` | `GET /v1/convai/conversations/{id}` | Yes |
| Retell | `retell.py` | `GET /v2/get-call/{call_id}` | **No** |
| Vapi | `vapi.py` | `GET /call/{id}`, or the `end-of-call-report` webhook body | **No** |

The Retell and Vapi adapters were written from each platform's published
reference, read on 24 November 2026. Their tests use payloads built by hand from
those references (`tests/fixtures/*_doc_derived.json`). Nobody has yet compared
either adapter with a payload from a live account, so treat both as unproven
until the first real call is read with them. Neither is wired into the command
line yet; they are used from Python.

## What each platform gives

| | ElevenLabs | Retell | Vapi |
| --- | --- | --- | --- |
| Tool name and arguments | Yes | Yes (`tool_call_invocation`) | Yes (`toolCalls[].function`) |
| Tool result | Yes | Yes (`tool_call_result.content`) | Yes (`result`) |
| Success flag from the platform | `is_error` | `successful`, when set | None |
| Time of a tool call | Time of the turn | None on the tool entry | `secondsFromStart` |
| Flow steps | Workflow step ids | `node_transition` on a flow agent | None on a message |
| Why the call ended | `termination_reason` | `disconnection_reason` | `endedReason` |

## What each platform does not give, and what the adapter does about it

The rule for every adapter: a gap is marked, never filled with a guess.

**Retell**

- A tool entry has no time. `ToolCall.t` is the end of the last word spoken
  before the call, which is a lower bound; it is `None` when nothing was spoken
  first.
- With the agent's data storage set to `basic_attributes_only`, no transcript is
  kept. `parse_call` raises `TraceUnavailableError`.
- With `everything_except_pii`, only the `scrubbed_` list may be present. The
  adapter uses it. Argument values in it may be masked by Retell, so a "stored"
  check on a masked field cannot be trusted.
- Tools that run before or after the conversation
  (`pre_session_…`/`post_session_transcript_with_tool_calls`) are not read.

**Vapi**

- There is no success flag. `ToolCall.ok` is read from the tool's own reply:
  `success: false`, `status: "error"` or an `error` value means failed; anything
  else, including plain text, counts as succeeded.
- Messages are present only if the assistant's artifact plan keeps them and the
  account is not on zero data retention. With no message list,
  `parse_call` raises `TraceUnavailableError`.
- There is no flow step on a message, so `step_path` is empty and step-path
  expectations cannot be graded.
- The webhook is sent once by default and can arrive out of order with other
  messages. For a test run, fetching the call by id is the steadier source.

**Both**

- A tool request with no result is kept, with `result` set to `"unavailable"`,
  `ok` false and an `error` that begins `unavailable:`. `ok` is false so an
  unknown outcome can never pass as a success.
- Arguments that are missing or not a JSON object become
  `{"_unavailable": "<reason>"}`.
- `common.unavailable_calls(trace)` lists the calls with either gap.

One known limit: the grader's success check is yes or no. A scenario that
expects a tool to be *refused* would pass on a call whose result is unavailable.
Until the grader has a third state, check `unavailable_calls` before trusting
such a pass.

A tool that answers "booked" is still not proof of a booking. On every platform
the tool result shows what the agent was told, not what the system stored.

## Adding another platform

1. Read the platform's reference for a finished call and note the exact field
   names, with the date you read them. Do not write from memory.
2. Add `traces/<platform>.py` with:
   - a pure function `parse_call(payload, call_ref="") -> Trace`, with no network;
   - a reader class with `async def read(self, call_ref) -> Trace` that fetches
     the call, waits until the platform reports it finished, and raises when it
     cannot be read.
3. Use `traces/common.py` for gaps: `missing_result`, `arguments_of`,
   `TraceUnavailableError`. Raise when the platform kept nothing; return an
   empty trace when the call simply used no tools. The two must not be confused.
4. Add a fixture under `tests/fixtures/`. Say in its `_provenance` field whether
   it was captured from a real call or built from documentation, and remove
   anything personal from a captured one.
5. Test the pairing of requests with results, a request with no result, and the
   "nothing kept" case. Test the reader with `httpx.MockTransport`.
6. Add the platform to the tables above, with what it does not give.
7. List the module in `traces/__init__.py`.
