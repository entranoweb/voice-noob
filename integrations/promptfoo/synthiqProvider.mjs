/**
 * A promptfoo custom provider that drives a real voice-agent run.
 *
 * promptfoo grades what a model said. This provider adds what the agent
 * actually did: the agent's own tools execute against a real database, and the
 * database is read afterwards to decide whether the work happened. The run is
 * wrapped in a transaction that is rolled back and verified, so grading a
 * hundred prompt variants leaves the data exactly as it was found.
 *
 * The transcript is returned as `output` so every promptfoo assertion —
 * contains, llm-rubric, moderation, the red-team plugins — works unchanged.
 * The deterministic verdict rides in `metadata`, where `javascript` and
 * `is-json` assertions can reach it.
 *
 * Usage in promptfooconfig.yaml:
 *
 *   providers:
 *     - id: file://./synthiqProvider.mjs
 *       config:
 *         apiBaseUrl: http://localhost:8000
 *         agentId: 0b7f...            # the agent under test
 *         invokes: [book_appointment] # tools the run must call
 *         leaves:                     # state the run must produce
 *           appointments:
 *             - status: scheduled
 */

const DEFAULT_BASE_URL = "http://localhost:8000";

class SynthiqProvider {
  constructor(options = {}) {
    this.providerId = options.id || "synthiq";
    this.config = options.config || {};
  }

  id() {
    return this.providerId;
  }

  /**
   * promptfoo passes the rendered prompt as the caller's turns.
   *
   * A single string is one turn. A JSON array of strings is a multi-turn
   * conversation, which is how a scenario with follow-ups is expressed without
   * needing a second prompt format.
   */
  #turns(prompt) {
    if (Array.isArray(prompt)) return prompt.map(String);
    const text = String(prompt ?? "").trim();
    if (text.startsWith("[")) {
      try {
        const parsed = JSON.parse(text);
        if (Array.isArray(parsed)) return parsed.map(String);
      } catch {
        // Not JSON after all — treat it as what it looks like, one turn.
      }
    }
    return [text];
  }

  async callApi(prompt, context) {
    const cfg = { ...this.config, ...(context?.vars?.synthiq ?? {}) };
    const baseUrl = (
      cfg.apiBaseUrl ||
      process.env.SYNTHIQ_API_URL ||
      DEFAULT_BASE_URL
    ).replace(/\/$/, "");
    const apiKey = cfg.apiKey || process.env.SYNTHIQ_API_KEY;

    if (!cfg.agentId) {
      return {
        error: "synthiq provider needs config.agentId — the agent to test",
      };
    }
    if (!apiKey) {
      return {
        error: "synthiq provider needs config.apiKey or SYNTHIQ_API_KEY",
      };
    }

    const body = {
      agent_id: cfg.agentId,
      says: this.#turns(prompt),
      invokes: cfg.invokes ?? [],
      leaves: cfg.leaves ?? null,
      given: cfg.given ?? null,
      persona: cfg.persona ?? null,
      max_response_ms: cfg.maxResponseMs ?? null,
      workspace_id: cfg.workspaceId ?? null,
      judge: cfg.judge ?? false,
    };

    let response;
    try {
      response = await fetch(`${baseUrl}/api/v1/testing/check`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiKey}`,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(cfg.timeoutMs ?? 120000),
      });
    } catch (err) {
      // A transport failure is a harness problem, not a finding about the
      // agent. promptfoo shows `error` distinctly from a failed assertion.
      return { error: `synthiq request failed: ${err.message}` };
    }

    if (!response.ok) {
      const detail = await response.text().catch(() => response.statusText);
      return {
        error: `synthiq returned ${response.status}: ${detail.slice(0, 500)}`,
      };
    }

    const result = await response.json();

    // The transcript is the output so text assertions work unchanged. The
    // verdict goes in metadata rather than being baked into the output string,
    // because a `javascript` assertion can read structure and cannot parse
    // prose reliably.
    return {
      output: result.transcript
        .map(
          (turn) =>
            `${String(turn.speaker).toUpperCase()}: ${turn.message ?? ""}`,
        )
        .join("\n"),
      metadata: {
        outcome: result.outcome,
        passed: result.passed,
        // An untrustworthy run is one nothing could be measured on. Assert on
        // this before asserting on `passed`, or a broken harness reads as a
        // failing agent.
        trustworthy: result.trustworthy,
        accuracyScore: result.accuracy_score,
        explanation: result.explanation,
        toolsInvoked: result.tool_calls.map((call) => call.name),
        toolCalls: result.tool_calls,
        finalState: result.final_state,
        fixtureLedger: result.fixture_ledger,
        metrics: Object.fromEntries(
          result.metrics.map((m) => [
            m.metric,
            { value: m.value, passed: m.passed, ...m.detail },
          ]),
        ),
        judgement: result.judgement,
      },
    };
  }
}

export default SynthiqProvider;
