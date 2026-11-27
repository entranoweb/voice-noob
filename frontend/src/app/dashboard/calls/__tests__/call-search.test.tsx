/**
 * The calls page search box asks the server, so it finds calls on every page
 * and the total reflects the matches, not just the twenty rows on screen.
 */
import { describe, it, expect, vi } from "vitest";
import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";
import { render, screen, waitFor, server } from "@/test/test-utils";
import CallHistoryPage from "../page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const API = "http://localhost:8000";

function callRow(id: string, agentName: string) {
  return {
    id,
    provider: "telnyx",
    provider_call_id: `v3:${id}`,
    agent_id: "agent-1",
    agent_name: agentName,
    contact_id: null,
    contact_name: null,
    workspace_id: null,
    workspace_name: null,
    direction: "inbound",
    status: "completed",
    from_number: "+441632960001",
    to_number: "+441632960002",
    duration_seconds: 30,
    recording_url: null,
    transcript: null,
    started_at: "2026-11-20T10:00:00Z",
    answered_at: null,
    ended_at: null,
  };
}

describe("Calls page search", () => {
  it("sends the typed text to the server and shows what it returns", async () => {
    const searches: (string | null)[] = [];
    server.use(
      http.get(`${API}/api/v1/workspaces`, () => HttpResponse.json([])),
      http.get(`${API}/api/v1/calls`, ({ request }) => {
        const search = new URL(request.url).searchParams.get("search");
        searches.push(search);
        const calls = search ? [callRow("c2", "Billing Line")] : [callRow("c1", "Front Desk")];
        return HttpResponse.json({
          calls,
          total: calls.length,
          page: 1,
          page_size: 20,
          total_pages: 1,
        });
      })
    );

    render(<CallHistoryPage />);
    expect(await screen.findByText("Front Desk")).toBeInTheDocument();

    await userEvent.type(
      screen.getByRole("textbox", { name: /search calls by agent, contact or number/i }),
      "billing"
    );

    expect(await screen.findByText("Billing Line")).toBeInTheDocument();
    expect(screen.queryByText("Front Desk")).not.toBeInTheDocument();
    await waitFor(() => expect(searches).toContain("billing"));
    expect(searches[0]).toBeNull();
  });
});
