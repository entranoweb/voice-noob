# Engineering decisions

Why the QA harness is built the way it is. Each entry exists because the obvious
alternative is wrong in a way that is not obvious, and someone will otherwise
"simplify" it back.

---

## 1. `value=None` means *not measurable*. `0.0` means *measured and bad*.

`MetricScore.value` is `float | None`, and the distinction is load-bearing.

Before this existed, a JSON parse failure in the judge was written to the
database as `score: 50, passed: False` — below the pass threshold — so **every
harness malfunction raised a failure alert about an agent that may have
performed perfectly.**

Any metric that cannot compute must return `not_measurable(reason)`. Never zero.
Zero is a measurement.

## 2. A broken run reports `ERROR`, never `FAILED`

`RunOutcome` has three values, not two. Validation-category metrics run first
and gate everything after them; if the run is not trustworthy, the accuracy
result is not reported as an agent result.

`TestRun.passed` is set to `None`, not `False`, when the run is untrustworthy.
An unmeasured run is not a failed one.

## 3. Deterministic assertions decide the verdict; the judge does not

Scenarios previously declared `min_score` and `must_invoke_tools`, and both were
serialised into the judge's prompt as prose. The judge returned its own `passed`
and the runner stored it unchallenged. The criteria were never enforced.

Order is: validation gates → accuracy decides → experience and diagnostic are
reported but do not decide. A slow-but-correct run passes; that distinction is
real and worth keeping.

## 4. Tools execute for real. Nothing is mocked.

Asserting on a mocked tool response only tests that the model said the right
words. The whole differentiator is asserting on what the database looks like
afterwards.

Third-party integrations (GoHighLevel, Calendly, Shopify, SMS) are excluded by
passing **no credentials** to `ToolRegistry`, so `get_all_tool_definitions`
skips them even when the agent has them enabled. A test run must never text a
real customer.

## 5. `join_transaction_mode="create_savepoint"` — the load-bearing line

In `fixtures.py`. The session handed to the agent's tools joins an outer
transaction in savepoint mode, so the `commit()` calls inside `CRMTools`
release a savepoint instead of writing durably.

**If this regresses, every simulated booking becomes a real one.**

Rollback by transaction, not by deleting what we think we created. Tracked
deletion misses cascades, cannot undo an *update* to a pre-existing row, and
leaves residue exactly when a run crashed halfway — which is when residue
matters most. There is a test for the crash case.

## 6. Rollback is verified, not assumed

After the transaction is gone, a **fresh connection** re-reads the database and
diffs it against a baseline taken before the run. Reading through the same
session could be answered from its identity map, which proves nothing about what
is on disk.

Three states stay distinct, for the same reason as decision 1: rollback failed /
rollback happened but nothing checked it / rollback verified clean. Only the
third is evidence, and only the third is what an audit record can claim.

## 7. The engine comes from the session's bind, not the module

`_engine_of(session)` rather than importing `app.db.session.engine`. Importing
it directly would have a run pointed at a staging or test database quietly seed
and roll back **somewhere else entirely** — which the first run of these tests
demonstrated by trying to reach port 5432 while the tests were on 5433.

## 8. `_execute` exists for its ordering

The state snapshot must happen **inside** the fixture scope, while the agent's
writes are still visible. The ledger is only complete **after** the scope exits
and rollback has been verified. That is the whole reason the method exists;
inlining it back into `run_scenario` loses the ordering.

## 9. Isolation is on by default

`run_scenario(isolated=True)`. This was a bug fix as much as a feature: once
tool binding landed, runs were executing real tools against the caller's real
database and leaving the rows there.

`isolated=False` remains available and reports `state_restored` as
**unmeasurable**, not clean — nothing checked it.

## 10. A scenario is an argument, not a row

`ScenarioSpec` builds a transient `TestScenario` that is never added to a
session. A suite must not accumulate scenario and run records as a side effect
of running.

`fixture` is its own column rather than another key inside `success_criteria`.
Fixtures are *setup*; confusing the world a test starts in with the world it
must end in is how a scenario ends up asserting something it also created.

## 11. `RunResult.__repr__` is the failure message

pytest prints the repr of a falsey object. `assert result` on a bare boolean
tells you a voice agent did something wrong somewhere, which is worthless at
2am. The repr names the failing metric, the state diff, the tools invoked, and
what the agent said.

An `ERROR` is falsey too — a test that could not measure anything must not
report success — but `explain()` says so in as many words.

## 12. Comparisons refuse to name a winner when intervals overlap

A voice agent is stochastic. One run against one run measures variance and
reports it as a difference, which is worse than not measuring: it manufactures
confidence.

`repeats` defaults to 5, not 1. Pass rates carry **Wilson** score intervals —
not the normal approximation, which returns a lower bound below zero for a
variant that passed every run — and `Comparison.winner()` returns `None` unless
the intervals separate. **That `None` is the designed outcome at small sample
sizes, not a gap.**

Errored runs count toward `repeats` but not toward the pass rate. An outage must
not decide a prompt comparison.

## 13. Mutations may only touch behaviour fields

`MUTABLE_FIELDS` is a fixed allowlist. A mutation is an experiment, not a way to
reassign an agent to another user or point it at a different phone number
mid-comparison.

Lists **replace** rather than concatenate on deep merge, otherwise removing a
tool would be impossible to express.

## 14. The simulated caller refuses to help

Two constraints in its prompt do most of the work:

- It must not volunteer information nobody asked for, and must not do the
  agent's job for it. A caller that fills in gaps the agent should have asked
  about tests nothing.
- It must not be unusually patient. It is a customer, not a tester.

It also does not decide whether the run passed. The caller talks; the metrics
judge. Letting it conclude "great, that worked" would put a model's opinion back
where deterministic assertions are supposed to sit.

Bounded three ways: a per-persona turn budget, `MAX_TURNS_CEILING` above it, and
a `<DONE>` sentinel. A final utterance arriving *alongside* the sentinel is
still spoken — "great, thanks, bye" is a real turn, and dropping it denies the
agent its chance to close the call.

A persona can declare a scripted `opening`, which skips the model for the first
turn. Two configurations under comparison must start identically or the
comparison measures the caller as much as the agent.

## 15. Three model roles, three settings

`QA_AGENT_MODEL`, `QA_CALLER_MODEL`, `QA_JUDGE_MODEL`. All three were one knob,
which cost roughly three times what the work requires.

Only the **agent** is the measurement — a weaker model there reports the agent
as worse than it is. The caller and judge default to Haiku.

`QA_OPEN_MODEL_BASE_URL` points caller and judge at any OpenAI-compatible
endpoint. This is not only a cost lever: a harness that must run inside a bank's
own network cannot depend on an external API for any part of a run.

Self-hosted models cost zero per token in the accounting, because the cost is
the machine. An invented per-token rate would misreport the API bill and the
infrastructure bill at once.

## 16. The two OpenAI Realtime namespaces emit different event names

`client.beta.realtime` sends `response.audio.delta`.
`client.realtime` sends `response.output_audio.delta`.

Renaming the handlers without moving the connect call would have silently killed
every call. They move together or not at all.

## 17. Tests run against Postgres, not SQLite

SQLite silently drops tzinfo from `DateTime(timezone=True)` and cannot compile
Postgres ARRAY columns. A suite run only on SQLite both fails tests that are
correct and hides bugs that are real. One schema per test, dropped afterwards.

## 18. Patch where a name is bound, not where it is defined

`test_runner` does `from app.services.qa.resilience import get_anthropic_client`,
so patching `app.services.qa.resilience.get_anthropic_client` rebinds nothing
and the code reaches for a live API key.

This has now bitten twice — once in `TestGetClient`, once in the inline-check
tests. Patch `app.services.qa.test_runner.get_anthropic_client`.

## 19. Shared state between tests must be reset explicitly

The rate limiter keys on remote address and every test client shares one, so a
test that deliberately exhausts a limit leaves every later test in the same
process getting 429s. Thirteen tests were failing for reasons unrelated to the
code they were in.

Same class of problem: `global.fetch` assigned at module scope in a vitest file
is replaced by MSW's `server.listen()` in a `beforeAll` from the shared setup,
so the mock never sees a call. Install per test, restore afterwards.

## 20. An exact-set assertion on the metric registry

`test_the_registered_set_is_exactly_what_we_expect` compares the full set, not a
subset. A metric that silently stops registering would drop out of every result
with nothing to notice it by. When you add a metric, this test failing is the
system working.

## 21. A Telnyx number can deliver either of two wire formats

Call Control posts JSON and expects API commands; TeXML posts form-encoded with
Twilio's parameter names and expects a document back. The inbound handlers read
JSON and answered with a document, which is a combination no account can be
configured to produce. `telnyx_events.parse_telnyx_webhook` accepts both, and
prefers the body's shape over the declared content type — a dropped call is a
worse outcome than trusting the bytes.

## 22. `bidirectionalMode="rtp"` is not optional

Telnyx defaults `<Stream>` to `mp3`. This bridge sends G.711 µ-law. On the
default the caller hears nothing while the logs show audio being written, which
is the hardest class of failure to find: everything on our side succeeds.

## 23. A caller with no script has no word error rate

`transcription_accuracy` compares what the caller meant to say against what STT
heard. A human calling a real number supplies only the second. The recorder
therefore leaves `text_intended` empty on a live caller turn, and
`turns_from_conversation` no longer falls back to the turn's message when a turn
declares its audio text explicitly.

Without that, intended would equal transcribed and every real call would score a
flawless 0.0 — the metric reporting perfection exactly where it has measured
nothing. `value=None` means not measurable; `0.0` means measured and bad.

## 24. Time to first audio is measured to the bytes, not to the intent

The clock starts when the caller stops speaking and stops at the first audio
chunk written toward the carrier, because that is the only moment the caller can
hear. An opening greeting answers no caller turn, so it records `None` rather
than a latency nobody waited through.

On this bridge `response_ms` and `ttfb_ms` are the same number: there is one
observation point. Recording one measurement under both of the schema's names is
honest; subtracting an invented constant to make them differ would not be.

## 25. `interrupted` comes from the provider, `barge_in` from the timing

The audio stops either way, so timing cannot tell a turn that finished from one
that was cut off. `barge_in` is ours to observe — the caller started speaking
while the agent was still producing audio. `interrupted` is only known from the
`response.done` the provider sends with a cancelled status.

They are also recorded against different turns' worth of evidence and must not
be collapsed: a barge-in the agent stopped for is good behaviour; a barge-in it
talked through is the failure.

## 26. `call_trace.py` was a vocabulary with no producer

It defined `voice.call`, `voice.turn` and `voice.tool_call` in full, and nothing
in the codebase emitted a span. `call_trace_emitter.py` is the producer, talking
to the OpenTelemetry API only — with no provider configured the API returns
non-recording spans, so no call site needs to check whether tracing is on.

Talking to the API alone would have left the emitter a no-op everywhere, so
`monitoring/tracing.py` installs an SDK provider and OTLP/HTTP exporter at
startup and flushes on shutdown. It reads `OTEL_ENABLED` and
`OTEL_EXPORTER_OTLP_ENDPOINT`, which had existed in settings since long before
any of this and were read by nothing. With `OTEL_ENABLED` false the emitter is
a no-op by design, and the startup log says which of the two it is — a tracing
feature that silently exports nothing is the failure this repository exists to
prevent.


## 27. A real call is measured, not graded

`MetricRunner.run` reports an error when no accuracy metric was measurable. That
is right for a scenario: it means the harness misbehaved and the other numbers
should not be read. It is wrong for a call that actually happened, which carries
no scenario, so `task_completion` is unmeasurable by construction.

Applied there, the gate stamped every genuine call `error` / not trustworthy and
threw away the latency and interruption figures alongside it — the one flag that
tells a consumer to ignore data, attached to the only data the path produces.
`RunOutcome.OBSERVED` and `evaluate_observed` keep the validation gates and drop
the verdict.

## 28. The terminal status of a call does not say who ended it

A call that reached `completed` may have been ended by the caller, by the agent,
or by a duration cap. Mapping the status to a termination reason invents one of
the three and puts it into every dashboard that groups by it.

Only the media bridge observes this, so it writes what it saw to
`call_records.termination_reason`. Null means nothing recorded a reason, which
the validation gate reads as a run not worth scoring. That is the honest
reading: we do not know how it ended.

## 29. A default on a security predicate is a hole with a timer on it

`save_transcript_to_call_record` took `agent_id` with a `None` default so the
Telnyx path could be scoped without touching the Twilio one. The Twilio call
site kept the unscoped signature, and with it the same cross-agent write.

The parameter is required now. A scoping predicate that can be omitted will be
omitted, by the next call site or the next author.

## 30. Refuse cleartext trace export, don't warn about it

The first version of `tracing.py` logged `tracing_endpoint_is_cleartext` when
`OTEL_EXPORTER_OTLP_ENDPOINT` was plain `http://` to a remote host, then built
the exporter and reported success. A call trace carries transcripts and the tool
arguments built from them, so that log line announced that caller speech was
going onto the wire in the clear and did nothing about it. A warning on a
data-exposure path is a note that the exposure is happening, not a control.

`_transport_is_permitted` decides before the exporter exists. HTTPS passes.
Plain HTTP to loopback passes — the sidecar-on-localhost collector is the
ordinary OpenTelemetry deployment and never leaves the host. Plain HTTP anywhere
else is refused: `configure_tracing` logs
`tracing_refused_cleartext_endpoint` and returns false, so the operator gets
no tracing rather than silent exposure.

The case that needed a decision rather than a rule is a collector reached over
HTTP across a private network — `collector.observability.svc:4318` is a real
deployment and a real exposure at once, and only the operator knows whether that
network is trusted. `OTEL_ALLOW_INSECURE_EXPORT` is how they say so; it still
logs the cleartext warning, because now the warning is accurate about what was
chosen.

## 31. `<Connect>` runs the next instruction, so the next instruction is `<Hangup/>`

The answer document ended `<Connect><Stream/></Connect><Pause length="40"/>`,
copied from a reference implementation as a guard against Connect returning
early. Telnyx documents `<Connect>` as running the next instruction only once
the connected service stops, so the pause guards nothing — it is forty seconds
of dead air after every conversation, on a leg the caller is still paying for
and still holding.

`<Hangup/>` closes the leg the moment the stream ends. Anything between the two
is silence the caller listens to after the call is over.

The wider point is why this survived: it was in the document from the first
version, and nothing had ever placed a call, so nothing had ever waited through
it. A test that asserts the document parses would still pass today.

## 32. Redaction covers the tool arguments, not just the speech

An agent with `enable_transcript` false sets `retain_text=False` on the
recorder. The first version dropped `text_intended` and `text_transcribed` and
stopped there, which reads as complete and is not: a booking's tool arguments
carry the caller's name, number and address, and a tool error carries whatever
it was called with. Redacting the transcript and keeping the arguments derived
from it protects the wording and leaks the content.

`retain_text=False` now drops the turn text, the tool `arguments` and the tool
`error`. The trace goes through the same recorder, so it is redacted by the
same switch rather than by a second one that can drift.

This is the third time in this work that a fix landed on one instance of a class
and left the rest — the OTLP path but not its query string, the Telnyx write
scoping but not the Twilio one. Worth stating as a habit to distrust.

## 33. The call identifier travels in the stream URL

The webhook knows the call by the identifier Telnyx signed the request with. The
media stream announces a `call_control_id` in its start frame. On a TeXML
application those are not guaranteed to be the same string, so the bridge cannot
reliably find the row the webhook created by taking the stream's word for it.
`build_telnyx_stream_url` puts `call_id` in the query string, and the socket
matches on it *and* on the serving agent — the identifier arrives over an
unauthenticated socket, so on its own it is a write primitive, not a lookup key
(see §29).

`direction` travels with it because inbound and outbound calls share the
endpoint. A trace that labels every outbound call inbound is worse than one
carrying no direction at all.

## 34. The websocket tests speak ASGI in the test's own event loop

`TestClient` drives the application from a second thread with its own event
loop, which does not mix with an asyncpg engine created in the test's loop — so
a websocket test using it cannot share the database fixtures every other test
uses, and the end-to-end inbound call test needs exactly that: the real row, in
the real transaction, rolled back with the rest.

`tests/websocket/asgi_ws.py` calls the ASGI application in the running loop
instead. It is a client, not a mock: real routing, real dependency graph, real
endpoint function. The alternative — a second engine for websocket tests — buys
a passing test that no longer proves the write happened where the application
would have written it.
