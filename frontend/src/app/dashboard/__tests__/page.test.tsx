import { describe, it, expect } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, server } from "@/test/test-utils";
import DashboardPage from "../page";

const API = "http://localhost:8000/api/v1";

function recentCall(index: number, status: string, seconds: number) {
  return {
    id: `00000000-0000-0000-0000-00000000000${index}`,
    provider: "telnyx",
    provider_call_id: `v3:${index}`,
    agent_id: null,
    agent_name: "Receptionist",
    contact_id: null,
    contact_name: null,
    workspace_id: null,
    workspace_name: null,
    direction: "inbound",
    status,
    from_number: "+15550000000",
    to_number: "+15550000001",
    duration_seconds: seconds,
    recording_url: null,
    transcript: null,
    started_at: "2026-11-25T09:00:00Z",
    answered_at: null,
    ended_at: null,
  };
}

/** The five newest calls all failed at 0 s; across all twelve calls, nine completed. */
function mockDashboard(stats: Record<string, number> | null) {
  server.use(
    http.get(`${API}/agents`, () => HttpResponse.json([])),
    http.get(`${API}/workspaces`, () => HttpResponse.json([])),
    http.get(`${API}/crm/appointments`, () => HttpResponse.json([])),
    http.get(`${API}/calls/stats`, () =>
      stats === null
        ? HttpResponse.json({ detail: "boom" }, { status: 500 })
        : HttpResponse.json(stats)
    ),
    http.get(`${API}/calls`, () =>
      HttpResponse.json({
        calls: [1, 2, 3, 4, 5].map((n) => recentCall(n, "failed", 0)),
        total: 12,
        page: 1,
        page_size: 5,
        total_pages: 3,
      })
    )
  );
}

describe("dashboard figures", () => {
  it("counts completed calls and averages duration over every call, not the five shown", async () => {
    mockDashboard({
      total_calls: 12,
      completed_calls: 9,
      inbound_calls: 12,
      outbound_calls: 0,
      total_duration_seconds: 960,
      average_duration_seconds: 80,
    });

    render(<DashboardPage />);

    expect(await screen.findByText("9 of 12")).toBeInTheDocument();
    expect(screen.getByText("1m 20s")).toBeInTheDocument();
    expect(screen.getByText("Completed (all calls)")).toBeInTheDocument();
    expect(screen.getByText("Avg Duration (all calls)")).toBeInTheDocument();
    // The five-call sample would have said 0 of 5 and 0s.
    expect(screen.queryByText("0 of 5")).not.toBeInTheDocument();
  });

  it("shows no figure rather than a wrong one when the totals cannot be loaded", async () => {
    mockDashboard(null);

    render(<DashboardPage />);

    expect(await screen.findAllByText("-")).not.toHaveLength(0);
    expect(screen.queryByText("0 of 5")).not.toBeInTheDocument();
  });
});
