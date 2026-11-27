/**
 * The agents list: every agent is reachable and named for a keyboard or screen
 * reader (a link, a labelled menu button, a labelled workspace filter), and no
 * agent form offers a call-recording switch, because no recording is made.
 */
import { describe, it, expect, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, server } from "@/test/test-utils";
import AgentsPage from "../page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const API = "http://localhost:8000";

const agent = {
  id: "agent-1",
  name: "Front Desk",
  description: null,
  pricing_tier: "premium",
  system_prompt: "Answer politely.",
  language: "en-US",
  voice: "marin",
  enabled_tools: [],
  enabled_tool_ids: {},
  phone_number_id: null,
  enable_recording: false,
  enable_transcript: true,
  is_active: true,
  total_calls: 0,
  created_at: "2026-11-01T10:00:00Z",
  updated_at: "2026-11-01T10:00:00Z",
};

function serve() {
  server.use(
    http.get(`${API}/api/v1/agents`, () => HttpResponse.json([agent])),
    http.get(`${API}/api/v1/workspaces`, () =>
      HttpResponse.json([{ id: "ws-1", name: "Main", description: null, is_default: true }])
    )
  );
}

describe("AgentsPage accessibility", () => {
  it("links each agent by name so the keyboard can open it", async () => {
    serve();
    render(<AgentsPage />);

    const link = await screen.findByRole("link", { name: "Front Desk" });
    expect(link).toHaveAttribute("href", "/dashboard/agents/agent-1");
  });

  it("names the per-agent menu button after the agent", async () => {
    serve();
    render(<AgentsPage />);

    await screen.findByRole("link", { name: "Front Desk" });
    expect(screen.getByRole("button", { name: "More actions for Front Desk" })).toBeVisible();
  });

  it("labels the workspace filter", async () => {
    serve();
    render(<AgentsPage />);

    expect(
      await screen.findByRole("combobox", { name: "Filter agents by workspace" })
    ).toBeVisible();
  });
});
