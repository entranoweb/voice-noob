/**
 * The call page behind /dashboard/calls/{id}. Three places linked to it before
 * it existed. It reads the two routes the backend serves for one call:
 * GET /api/v1/calls/{id} and GET /api/v1/calls/{id}/metrics.
 */
import { describe, it, expect, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, server } from "@/test/test-utils";
import CallDetailPage from "../[id]/page";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "call-1" }),
  useRouter: () => ({ push: vi.fn() }),
}));

const API = "http://localhost:8000";

const call = {
  id: "call-1",
  provider: "telnyx",
  provider_call_id: "v3:abc",
  agent_id: "agent-1",
  agent_name: "Reception",
  contact_id: null,
  contact_name: null,
  workspace_id: "ws-1",
  workspace_name: "Default Workspace",
  direction: "inbound",
  status: "completed",
  from_number: "+441632960001",
  to_number: "+441632960002",
  duration_seconds: 75,
  recording_url: null,
  transcript: "Caller: hello\nAgent: hello, how can I help?",
  started_at: "2026-11-20T10:00:00Z",
  answered_at: "2026-11-20T10:00:02Z",
  ended_at: "2026-11-20T10:01:17Z",
};

const metrics = {
  call_id: "call-1",
  outcome: "passed",
  trustworthy: true,
  has_audio: true,
  scores: [
    {
      metric: "response_latency",
      version: "1",
      category: "latency",
      kind: "measured",
      value: 0,
      passed: true,
      unit: "ms",
      detail: {},
    },
    {
      metric: "word_error_rate",
      version: "1",
      category: "accuracy",
      kind: "measured",
      value: null,
      passed: null,
      unit: null,
      detail: {},
    },
  ],
  invalid_reasons: [],
};

describe("CallDetailPage", () => {
  it("shows the call, its transcript and what was measured", async () => {
    server.use(
      http.get(`${API}/api/v1/calls/call-1`, () => HttpResponse.json(call)),
      http.get(`${API}/api/v1/calls/call-1/metrics`, () => HttpResponse.json(metrics))
    );

    render(<CallDetailPage />);

    expect(await screen.findByRole("heading", { name: /call with \+441632960001/i })).toBeVisible();
    expect(screen.getByText("1m 15s")).toBeInTheDocument();
    expect(screen.getByText(/Agent: hello, how can I help\?/)).toBeInTheDocument();
    expect(screen.getByText("No recording was kept for this call.")).toBeInTheDocument();
    expect(await screen.findByText("response latency")).toBeInTheDocument();
  });

  it("keeps a measured zero apart from a value that could not be measured", async () => {
    server.use(
      http.get(`${API}/api/v1/calls/call-1`, () => HttpResponse.json(call)),
      http.get(`${API}/api/v1/calls/call-1/metrics`, () => HttpResponse.json(metrics))
    );

    render(<CallDetailPage />);

    const latencyRow = (await screen.findByText("response latency")).closest("tr");
    const werRow = screen.getByText("word error rate").closest("tr");
    expect(latencyRow).toHaveTextContent("0 ms");
    expect(latencyRow).toHaveTextContent("Pass");
    expect(werRow).toHaveTextContent("Not measurable");
    expect(werRow).toHaveTextContent("No verdict");
  });

  it("says so when the call does not exist", async () => {
    server.use(
      http.get(`${API}/api/v1/calls/call-1`, () =>
        HttpResponse.json({ detail: "Call record not found" }, { status: 404 })
      )
    );

    render(<CallDetailPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Call record not found");
  });
});
