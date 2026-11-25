import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";
import { render, screen, server, waitFor, within } from "@/test/test-utils";
import type { CallerCallDetail } from "@/lib/api/caller-runs";
import CallerCallPage from "../[runId]/calls/[callId]/page";

const route = vi.hoisted((): { params: Record<string, string> } => ({ params: {} }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn(), back: vi.fn() }),
  usePathname: () => "/dashboard/caller-runs",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => route.params,
}));

const API = "http://localhost:8000/api/v1/caller/runs";
const RUN_ID = "11111111-1111-1111-1111-111111111111";
const CALL_ID = "22222222-2222-2222-2222-222222222222";
const audioUrl = (track: string) => `/api/v1/caller/runs/${RUN_ID}/calls/${CALL_ID}/audio/${track}`;

const call: CallerCallDetail = {
  id: CALL_ID,
  run: { id: RUN_ID, run_id: "run-a", target_label: "Receptionist, test copy" },
  position: 1,
  file: "run-001.json",
  scenario: "books-a-visit",
  scenario_file: "books-a-visit.json",
  mutates: "none",
  repeat: 1,
  outcome: "passed",
  fault_class: null,
  last_step: "",
  conversation_id: null,
  ended: "agent ended the call",
  duration_s: 20,
  excluded_reason: null,
  fields: [],
  checks: [],
  timeline: [
    { t: 0.4, kind: "agent", text: "How can I help?" },
    { t: 6.5, kind: "said", clips: ["book.wav"] },
  ],
  tool_calls: [{ name: "find_slot", arguments: {}, result: null, ok: true, t: 12.25 }],
  said: [{ t: 6.5, intended: "I would like a visit" }],
  step_path: [],
  read_back: null,
  synthetic_clips: [],
  labels_unverified: [],
  notes: {},
  audio: [
    {
      track: "caller",
      url: audioUrl("caller"),
      duration_s: 20,
      size_bytes: 640044,
      sample_rate: 16000,
    },
    {
      track: "agent",
      url: audioUrl("agent"),
      duration_s: 20,
      size_bytes: 640044,
      sample_rate: 16000,
    },
  ],
};

// jsdom does not play media. These stand in for the element's own members, so that
// what the page asks of the element (a position, a play) can be read back.
const played = vi.hoisted(() => ({ positions: [] as number[], plays: 0 }));

beforeEach(() => {
  route.params = { runId: RUN_ID, callId: CALL_ID };
  played.positions = [];
  played.plays = 0;
  Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
    configurable: true,
    get: () => played.positions.at(-1) ?? 0,
    set: (value: number) => {
      played.positions.push(value);
    },
  });
  HTMLMediaElement.prototype.play = vi.fn(() => {
    played.plays += 1;
    return Promise.resolve();
  });
  HTMLMediaElement.prototype.pause = vi.fn();
  URL.createObjectURL = vi.fn(() => "blob:test-audio");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function serve(detail: CallerCallDetail, authorised: string[] = []) {
  server.use(
    http.get(`${API}/${RUN_ID}/calls/${CALL_ID}`, () => HttpResponse.json(detail)),
    http.get(`${API}/${RUN_ID}/calls/${CALL_ID}/audio/:track`, ({ request }) => {
      authorised.push(request.headers.get("authorization") ?? "");
      return new HttpResponse(new Uint8Array([0x52, 0x49, 0x46, 0x46]), {
        headers: { "Content-Type": "audio/wav" },
      });
    })
  );
}

describe("call audio on the call page", () => {
  it("shows a player for each side, fetched as a file and played from a local address", async () => {
    serve(call);

    const { container } = render(<CallerCallPage />);

    await waitFor(() => expect(container.querySelectorAll("audio")).toHaveLength(2));
    expect(screen.getByText("Caller (what the rig played)")).toBeInTheDocument();
    expect(screen.getByText("Agent (what the agent said)")).toBeInTheDocument();
    const sources = Array.from(container.querySelectorAll("audio")).map((el) => el.src);
    expect(sources).toEqual(["blob:test-audio", "blob:test-audio"]);
  });

  it("sends the sign-in token with the audio request, since an audio element cannot", async () => {
    localStorage.setItem("access_token", "token-for-test");
    const seen: string[] = [];
    serve(call, seen);

    const { container } = render(<CallerCallPage />);

    await waitFor(() => expect(container.querySelectorAll("audio")).toHaveLength(2));
    expect(seen).toHaveLength(2);
    localStorage.removeItem("access_token");
  });

  it("marks where the caller's clip played, the agent spoke and a tool was called", async () => {
    serve(call);

    render(<CallerCallPage />);

    const group = await screen.findByRole("group", { name: "Markers on the recording" });
    const marks = within(group).getAllByRole("button");
    expect(marks.map((m) => m.getAttribute("data-marker"))).toEqual(["agent", "caller", "tool"]);
    expect(marks.map((m) => m.getAttribute("aria-label"))).toEqual([
      "agent spoke at 0.4 seconds",
      "caller clip played at 6.5 seconds",
      "tool call at 12.3 seconds",
    ]);
  });

  it("moves the recording to a row's time when the row is clicked, and plays it", async () => {
    serve(call);
    const user = userEvent.setup();

    const { container } = render(<CallerCallPage />);
    await waitFor(() => expect(container.querySelectorAll("audio")).toHaveLength(2));
    const timeline = screen.getByRole("list", { name: "Call timeline" });

    await user.click(within(timeline).getByRole("button", { name: /Play from 6\.5 seconds/ }));

    // Both sides are moved, so they stay in step.
    expect(played.positions).toEqual([6.5, 6.5]);
    expect(played.plays).toBe(1);
  });

  it("moves the recording when a marker is clicked", async () => {
    serve(call);
    const user = userEvent.setup();

    const { container } = render(<CallerCallPage />);
    await waitFor(() => expect(container.querySelectorAll("audio")).toHaveLength(2));

    await user.click(screen.getByRole("button", { name: "agent spoke at 0.4 seconds" }));

    expect(played.positions).toEqual([0.4, 0.4]);
    expect(played.plays).toBe(1);
  });

  it("says plainly that there is no audio, with no player and no seek buttons", async () => {
    serve({ ...call, audio: [] });

    const { container } = render(<CallerCallPage />);

    const note = await screen.findByText(/No audio is stored for this call\./);
    expect(note).toHaveTextContent("No audio is stored for this call.");
    expect(note).toHaveTextContent("not recorded or was not uploaded");
    expect(container.querySelector("audio")).toBeNull();
    expect(screen.queryByRole("group", { name: "Markers on the recording" })).toBeNull();
    const timeline = screen.getByRole("list", { name: "Call timeline" });
    expect(within(timeline).queryAllByRole("button")).toHaveLength(0);
    expect(within(timeline).getAllByRole("listitem")).toHaveLength(3);
  });

  it("says the recording expired, not that none was made, with no broken player", async () => {
    serve({ ...call, audio: [], audio_expired: true });

    const { container } = render(<CallerCallPage />);

    expect(await screen.findByText(/Recording expired\./)).toBeInTheDocument();
    expect(screen.queryByText(/No audio is stored/)).toBeNull();
    expect(container.querySelector("audio")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("treats a server that does not send the audio list as having none", async () => {
    const { audio: _omitted, ...older } = call;
    void _omitted;
    serve(older);

    render(<CallerCallPage />);

    expect(await screen.findByText(/No audio is stored for this call\./)).toBeInTheDocument();
  });

  it("says when a recording that should be there cannot be loaded", async () => {
    server.use(
      http.get(`${API}/${RUN_ID}/calls/${CALL_ID}`, () => HttpResponse.json(call)),
      http.get(`${API}/${RUN_ID}/calls/${CALL_ID}/audio/:track`, () =>
        HttpResponse.json({ detail: "Audio not found" }, { status: 404 })
      )
    );

    const { container } = render(<CallerCallPage />);

    const alerts = await screen.findAllByRole("alert");
    expect(alerts[0]).toHaveTextContent("could not be loaded");
    expect(container.querySelector("audio")).toBeNull();
  });
});
