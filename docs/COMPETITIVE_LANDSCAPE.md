# Competitive landscape — voice agent testing

**Researched:** 26–27 August 2026, across three research passes.
**Read this with the caveats section first.** Several figures come from vendor
marketing about their own competitors, and are flagged as such.

---

## The funded field

All four leaders are US-headquartered YC companies. None is open source. Every
one reserves private deployment for enterprise contracts.

| Vendor | Funding | Entry price | Private / VPC | Backend state |
| --- | --- | --- | --- | --- |
| **Coval** | $31M total; $28M Series A June 2026, Norwest, Base10, Twilio Ventures, YC. Founder previously led evaluation job infrastructure at Waymo. YC S24. ~60 customers incl. Zoom, Deepgram, Perplexity | $100/mo Starter (100 sim min, $0.40/min overage); $500 Growth | Enterprise, **from $4,500/mo** | `API State` metric — customer-built HTTP endpoint |
| **Hamming** | $3.8–4.3M seed, Dec 2024, Mischief. YC S24. Targets healthcare and finance | Contact sales — no public price at any tier | Single-tenant + residency; topology not public | Tool-call evidence, repeat-caller memory. Coval calls it "state-blind"; Hamming disputes |
| **Cekura** (was Vocera) | ~$2.9M. YC F24 | $30/mo Developer, or $0.25/voice-testing min | BYOC / VPC on custom annual contract | Rule-based conditional actions |
| **Roark** | YC W25 pre-seed. 10M+ call-minutes processed | Usage-based, contact sales | Not advertised | Production replay, goal/flow checks |

**A five-person agency running ~500 test minutes/month** pays roughly $245 on
Cekura or $260 on Coval. Undercutting that is not a business. The
discontinuity is elsewhere: the moment a buyer says the recordings cannot sit
in a multi-tenant cloud, the price jumps to $4,500/mo or an undisclosed annual
quote.

---

## Coval in detail

The most complete competitor, and the one to understand properly.

### Resource model

Derived from their public `coval-resources` skill (MIT, on GitHub):

```
Agent (22-char ID)
└── Mutation (26-char ID)
Test Set (8-char ID)
└── Test Case (22-char ID)
Persona (22-char ID)
Metric (22-char ID)
Run (22-char ID)
└── Simulation (22-char ID)
    └── Metric Output (26-char ID)
Run Template (22-char ID)
└── Scheduled Run (22-char ID)
```

API base `https://api.coval.dev/v1`, `X-API-Key` auth, AIP-160 filter syntax.

### Metric types

A separate research pass reported the public `MetricType` enum as exactly
twelve values:

`METRIC_LLM_BINARY`, `METRIC_CATEGORICAL`, `METRIC_NUMERICAL_LLM_JUDGE`,
`METRIC_AUDIO_LLM_BINARY`, `METRIC_AUDIO_LLM_CATEGORICAL`,
`METRIC_AUDIO_LLM_NUMERICAL`, `METRIC_TOOLCALL`, `METRIC_METADATA_FIELD`,
`METRIC_TRANSCRIPT_REGEX`, `METRIC_PAUSE_ANALYSIS`, `METRIC_SQL_FLOAT`,
`METRIC_COMPOSITE_EVALUATION`.

Built-in metrics include Latency, Turn Count, Audio Duration, Transcript
Sentiment, Speech Tempo, Time To First Audio, Interruption Rate, Background
Noise, Words Per Message.

**Note what is not in that enum:** no `METRIC_API_STATE`. Either it is
enterprise-gated, in a non-public API, or the "stateful workflow testing" they
market is a documentation pattern built from `METRIC_METADATA_FIELD` plus
customer-side HTTP calls. **This is unverified** — see caveats.

Note also that they *do* have deterministic metric types (regex, tool-call,
metadata, SQL, pause analysis). The claim "they have no deterministic metrics"
would be false. Our difference is the **gating order** — validation gates
first, accuracy decides, judge only for what remains — plus `ERROR` being
distinct from `FAILED`.

### What they have that we do not

- **Mutation** — A/B variants as a first-class resource, deep-merged JSON
  overrides at runtime, one active per agent/name pair. *(We built our own in
  `7ddf8cf`.)*
- **Test Set types `SCENARIO | TRANSCRIPT | AUDIO | SCRIPT`** — production calls
  replayed as regression tests. We only have SCENARIO.
- **Persona controls voice, interruption patterns, background noise,
  personality** — audio perturbation modelled into the persona primitive rather
  than bolted on separately. Better design than a separate perturbation layer.
- **Run Template → Scheduled Run**, cron and rate-based.
- **Human review** with human-vs-machine label agreement and automated metric
  prompt refinement — judge calibration, which we do not have.
- **Sofia** — a delegatable forward-deployed-engineer agent.
- **Dashboards, multi-run reports, alerts, webhooks, workspaces.**

### Their distribution strategy — worth copying

Three surfaces:

| | Purpose | Install |
| --- | --- | --- |
| **Agent Skills** | Knowledge — how to evaluate well | `npx skills add coval-ai/coval-external-skills` |
| **MCP connector** | Tools — execute operations | ChatGPT, Claude, Codex, any MCP client |
| **CLI** | Operations from a terminal | `brew install coval-ai/tap/coval` |

**The skills are MIT and open source while the product is closed.** They
open-sourced the *distribution layer*, not the engine. That move is available to
us in a stronger form: their skill teaches an agent to drive a SaaS; ours could
install the harness itself.

They also ship a `migrate-bluejay` skill — competitor migration tooling as a
skill. Cheap and aggressive.

---

## The framework-native harnesses

Each tests only agents built on itself.

- **Pipecat Evals** (BSD-2, shipped in Pipecat 1.4.0). YAML scenarios; text and
  **audio** mode. Audio mode synthesises the caller's voice, queues it on a
  virtual microphone at real-time cadence so the VAD path behaves, streams it
  through the agent's real STT, and transcribes what the agent actually said.
  Asserts on transcriptions, LLM responses, spoken output, function calls with
  latency budgets, and LLM-judged natural-language criteria. Connects over RTVI
  to a Pipecat transport (`-t eval`).
  **This is why audio is parity work for us, not a moat.**
- **LiveKit Agents evals** (Apache-2.0). pytest and vitest helpers, multi-turn
  behavioural assertions, `.judge(llm, {intent: …})`.
- **Vapi Simulations.** Mocks tool responses at the scenario level to avoid
  calling real APIs. Structural: they do not own your CRM. Third-party writeups
  name the consequence — *"if your tests only assert the happy path against a
  mocked backend, you will not see many failure modes."*

---

## Open-source, generic

| Tool | Stars | Licence | Voice-native? | Self-host? |
| --- | --- | --- | --- | --- |
| promptfoo | ~25k | MIT | No | Yes, local CLI |
| Langfuse | ~16–34k | MIT core, `ee/` commercial | No | Yes |
| DeepEval | ~14–18k | Apache-2.0 | No | Yes |
| Ragas | ~5–15k | Apache-2.0 | No | Yes |
| LangWatch / Scenario | growing | Open source | Partial — voice-to-voice CI | Yes |
| Fixa | — | MIT, YC | Yes | Yes — but deprioritised |
| ServiceNow EVA | — | Open source | Yes | Yes |

**promptfoo was acquired by OpenAI, 9 March 2026** — $23M raised, $86M pre-deal,
folding into OpenAI Frontier. MIT licence and public repo explicitly retained.
Reaches 150,000+ developers, ~25% of the Fortune 500. Ships red-team plugins
mapping to OWASP LLM Top 10, NIST AI RMF and EU AI Act guidance.

**Fixa is the cautionary tale.** Good open-source voice testing, MIT, YC-backed,
built by ex-PlayHT voice engineers — and its founders said publicly that the
monetisation model "didn't really make sense." It stalled. **EfficientAI** (MIT,
73 stars) is close to our architecture — FastAPI, Postgres, Redis, Celery,
React, personas, dialogue trees, batch audio — and nobody found it. Another
standalone dashboard is not a distribution strategy.

**Yapper** (MIT, 16 commits, v0.1.0) is the smallest and most strategically
interesting: it places a real phone call through Twilio and runs 20 attack
scenarios mapped to OWASP LLM Top 10 against a live voice agent. It is a toy,
and it is the only thing in this survey that red-teams a voice agent *over the
telephone network*.

---

## The engine layer

| Project | What | Licence | Read |
| --- | --- | --- | --- |
| `livekit/agents` | ~120 plugin packages: STT, TTS, LLM, VAD, turn detection, Krisp, and Telnyx | **Apache 2.0** | The provider-optionality layer, already built and permissively licensed |
| `pipecat-ai/pipecat` | Frames, pipelines, processors, transports. 60+ services, ~12–15k stars, maintained by Daily | **BSD-2** | We removed it as an unused dependency, not as an engine decision. Worth revisiting deliberately |
| `dograh-hq/dograh` | Self-hosted Vapi/Retell alternative built **on** Pipecat, pinned as a submodule. Visual workflow builder, QA node, BYOK across LLM/STT/TTS, telephony across Twilio/Vonage/**Telnyx**/Plivo/Vobiz/Cloudonix/Asterisk ARI, one-command Docker. ~5.5k stars | **BSD-2** | Closest thing to a working reference for the architecture we keep discussing, and we already borrow from it: `monitoring/loop_lag.py` credits its event-loop lag technique. Read it, do not adopt it — it is a competing *platform*, so taking it whole means discarding our own CRM, workspaces and embed. Note it pins Pipecat rather than tracking it, which is what "take upstream updates" actually costs |
| `rapidaai/voice-ai` | Go + gRPC orchestration, SIP, Postgres/Redis/OpenSearch, React console | **GPL-2.0 + visible-branding condition** | **Do not adopt.** See below |

### The Rapida licence trap

GitHub reports GPL-2.0 with a condition requiring visible Rapida branding in the
UI and a paid commercial licence to remove it or go closed-source. The entire
distribution model here is white-label and reseller. Copyleft plus mandatory
branding is structurally incompatible with that, and it is fixable only by not
starting. Have a lawyer confirm the exact terms before any of it reaches the
repo. Apache 2.0 and BSD-2 carry no such problem.

### pi.dev is a coding agent

`pi.dev` is the Pi coding agent (earendil-works / Mario Zechner): sessions as a
branching tree, `defineTool()`, built-ins read/write/edit/bash/grep, four
runtime modes. Nothing to do with evals. The useful Pi is a different company:
**Pi Labs' Pi Scorer** (withpi.ai) — a dedicated scoring model rather than a
prompted judge, 20+ dimensions in under 100ms, already a first-class promptfoo
assertion type. A candidate to replace our judge; the registry already separates
`DETERMINISTIC` from `JUDGE`, so it is a backend swap.

---

## The regulatory picture

Researched because "your recordings never leave your infrastructure" was
proposed as the moat. **It is a sharp wedge into a lucrative minority, not a
mass-market moat.**

### Against the sovereignty argument

- US hyperscalers held ~70% of the European cloud market in H1 2025; European
  providers ~15% (Synergy Research). Sovereignty rhetoric has not moved it.
- The EU-US Data Privacy Framework is **valid law in 2026**. For a DPF-certified
  US vendor, transfers are lawful today without SCCs.
- **EUCS had its sovereignty requirements removed** under industry and
  member-state pressure. EUCS "High" ≠ CLOUD Act immunity.
- NHS guidance explicitly permits approved US cloud with the right paperwork.
- AWS, Microsoft and Google all sell "sovereign cloud" offerings that many
  buyers accept.

### For it

- Voice recordings routinely contain Article 9 special-category data.
- **EDPB Recommendations 01/2020**: where a third-country processor needs data
  *in the clear* to provide the service, the EDPB "cannot envision an effective
  technical measure" preventing problematic access. A testing service must
  ingest audio in usable form to transcribe and grade it. Self-hosting sidesteps
  the transfer analysis entirely.
- **France, SecNumCloud 3.2** — explicit immunity-from-extraterritorial-law
  criteria; mandatory for sensitive French public-sector data. A US SaaS simply
  cannot be used there.
- **Germany, BSI C3A** — provider must be EU-controlled, 90 days' notice of
  ownership changes affecting sovereignty.
- **DORA** (applicable 17 Jan 2025) does *not* mandate localisation but requires
  the country of provision, storage and processing on the ICT register, plus
  audit rights and tested exit strategies. ECB 2025 guidance goes further:
  restrict permitted storage locations and weigh third-country legal exposure.
- **The DPF's foundation is cracking.** Latombe appealed to the CJEU on 31 Oct
  2025 (C-703/25 P), pending. The PCLOB — a load-bearing pillar of the adequacy
  decision — lost quorum in January 2025. Safe Harbor lasted 15 years, Privacy
  Shield 4.
- The European Commission itself was found in breach for transferring personal
  data to the US (General Court, 8 Jan 2025).

### The line that survives

Not: *"GDPR makes US voice-testing SaaS unlawful."* It does not.

Instead: *"For EU banks and public-sector buyers, a US-hosted vendor adds a
separate layer of transfer, data-location, subprocessor, audit and third-party
risk diligence. DORA does not mandate EU-only hosting and the DPF can provide a
lawful route — but ECB guidance asks banks to restrict where cloud data may be
stored and weigh third-country exposure, and EDPB guidance shows why ordinary
encryption does not cure the problem when the processor needs plaintext. A
customer-controlled deployment with customer-held keys materially reduces that
surface."*

Narrower, true, and much harder for an incumbent to wave away.

---

## The open-source GTM playbook

What actually drove adoption for the tools that inflected:

- **promptfoo** — CLI-first, YAML in-repo, ~15-minute first value, runs entirely
  locally with no account. Then expanded into red teaming and security.
- **DeepEval** — "pytest, but for LLMs." Borrowed a testing primitive developers
  already knew rather than teaching a new one. Open-core with a closed cloud.
- **Ragas** — owned one painful noun (RAG evaluation) before broadening.
  Distribution through LangChain/LlamaIndex integrations.
- **Langfuse** — observability first, so it runs continuously rather than in
  periodic bursts. Self-host in minutes. Open-sourced *all* product features in
  June 2025, keeping only SCIM and audit logs commercial.

**Common pattern:** permissive licence (never BSL) + local or self-hostable
first experience + a familiar developer workflow + one sharp wedge, not a broad
platform + distribution through CI and framework integrations.

---

## Caveats — read before quoting any of this

- **Coval's missing `METRIC_API_STATE` and the absence of any fixture/teardown
  primitive are inferred from what their public API omits.** Two independent
  passes reached it, but absence of evidence is not proof. Verify with
  `curl -s https://api.coval.dev/v1/openapi | jq .` before this appears in a
  sales conversation. `docs/COVAL_RECON_PROMPT.md` is the structured way to
  settle it.
- **Hamming being "state-blind" comes from Coval's own comparison page.**
  Hamming disputes it. Both are marketing.
- One earlier research pass produced a comparison table listing `setup_fixture`
  and `teardown_fixture` as capabilities *we* shipped, before they existed.
  Fabricated columns end up in decks and become false claims to customers. Any
  agent doing this research must be told to report on the competitor only.
- **Fixa's current status** rests on founder comments, not a commit history.
- Several claims about promptfoo, LiveKit, Pipecat and pi.dev internals come
  from search summaries, not primary documentation — those domains were
  unreachable from the research environment.
- Star counts are approximate 2026 snapshots from secondary sources, and vary
  between passes.
- Market-sizing figures ("$7B invested in voice AI in Q1 2026") originate in
  Coval's own fundraising PR. Treat as directional vendor marketing.
- The "Testing the Testers" arXiv study (2511.04133) scoring Coval 48.9 /
  Cekura 43.0 is cited by Coval and was not independently verified.
