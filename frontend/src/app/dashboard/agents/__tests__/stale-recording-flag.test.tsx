/**
 * An agent saved earlier with enable_recording=true still carries that promise.
 * No recording is made, so saving the agent sends false and clears it.
 */
import { Suspense } from "react";
import { describe, it, expect, vi } from "vitest";
import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";
import { render, screen, waitFor, server } from "@/test/test-utils";
import EditAgentPage from "../[id]/page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

/** A promise React's `use` reads synchronously, so the page needs no Suspense wait. */
const params = Object.assign(Promise.resolve({ id: "agent-1" }), {
  status: "fulfilled",
  value: { id: "agent-1" },
});

const API = "http://localhost:8000";

const agent = {
  id: "agent-1",
  name: "Front Desk",
  description: null,
  pricing_tier: "premium",
  system_prompt: "Answer the phone politely.",
  language: "en-US",
  voice: "marin",
  enabled_tools: [],
  enabled_tool_ids: {},
  phone_number_id: null,
  enable_recording: true,
  enable_transcript: true,
  is_active: true,
  total_calls: 0,
  temperature: 0.7,
  max_tokens: 2000,
  initial_greeting: null,
  created_at: "2026-11-01T10:00:00Z",
  updated_at: "2026-11-01T10:00:00Z",
};

describe("editing an agent that was saved with recording on", () => {
  it("sends enable_recording false on save", async () => {
    const bodies: Record<string, unknown>[] = [];
    server.use(
      http.get(`${API}/api/v1/agents/agent-1`, () => HttpResponse.json(agent)),
      http.put(`${API}/api/v1/agents/agent-1`, async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(agent);
      }),
      http.get(`${API}/api/v1/workspaces`, () => HttpResponse.json([])),
      http.get(`${API}/api/v1/workspaces/agent/agent-1`, () => HttpResponse.json([])),
      http.get(`${API}/api/v1/agents/agent-1/embed`, () => HttpResponse.json({})),
      http.patch(`${API}/api/v1/agents/agent-1/embed`, () => HttpResponse.json({})),
      http.put(`${API}/api/v1/workspaces/agent/agent-1/workspaces`, () => HttpResponse.json({}))
    );

    render(
      <Suspense fallback={null}>
        <EditAgentPage params={params} />
      </Suspense>
    );

    await userEvent.click(await screen.findByRole("button", { name: "Save Changes" }));

    await waitFor(() => expect(bodies.length).toBeGreaterThan(0));
    expect(bodies[0]?.enable_recording).toBe(false);
  });
});
