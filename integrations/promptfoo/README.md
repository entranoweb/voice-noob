# Synthiq provider for promptfoo

promptfoo grades what a model **said**. This provider adds what the agent
**did**.

A simulated call runs against your own database with your agent's real tools
bound — no mocked tool responses — and the database is read afterwards to
decide whether the work actually happened. The run is wrapped in a transaction
that is rolled back and verified, so grading a hundred prompt variants leaves
your data exactly as it found it.

## Install

Copy `synthiqProvider.mjs` next to your `promptfooconfig.yaml`, then:

```bash
export SYNTHIQ_API_KEY=...       # a Synthiq API token
npx promptfoo@latest eval
```

## Configure

```yaml
providers:
  - id: file://./synthiqProvider.mjs
    config:
      apiBaseUrl: http://localhost:8000   # or SYNTHIQ_API_URL
      agentId: 0b7f...                    # the agent under test
      invokes: [book_appointment]         # tools the run must call
      leaves:                             # state the run must produce
        appointments:
          - status: scheduled
      given:                              # rows seeded before the run
        contacts:
          - first_name: Jane
            phone_number: "5551234567"
      judge: false                        # qualitative judge, off by default
```

A prompt is one caller turn. A prompt that is a JSON array of strings is a
multi-turn conversation.

## What comes back

`output` is the transcript, so every promptfoo assertion — `contains`,
`llm-rubric`, `moderation`, the red-team plugins — works unchanged.

`metadata` carries the deterministic verdict:

| Key | Meaning |
| --- | --- |
| `outcome` | `PASSED`, `FAILED`, or `ERROR` |
| `trustworthy` | Whether anything could be measured at all |
| `metrics.task_completion` | Did the database end up as the scenario required |
| `metrics.expected_tools_invoked` | Were the required tools actually called |
| `metrics.state_restored` | Did the run roll itself back, verified |
| `toolsInvoked` | Tool names, in order |
| `finalState` | The database as it stood at the end of the run |
| `explanation` | A readable account of why it failed |

## Assert on measurability first

```yaml
- type: javascript
  value: output.metadata?.trustworthy === true
- type: javascript
  value: output.metadata?.metrics?.task_completion?.passed === true
```

`ERROR` means the run could not be measured — a harness problem, not an agent
one. Checking `passed` without checking `trustworthy` first turns every outage
into a false alarm about your agent.
