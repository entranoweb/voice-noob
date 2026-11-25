import { describe, it, expect, vi, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, server, within } from "@/test/test-utils";
import type {
  CallerCallDetail,
  CallerRunDetail,
  CallerRunSummary,
  Rate,
} from "@/lib/api/caller-runs";
import CallerRunsPage from "../page";
import CallerRunPage from "../[runId]/page";
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

const rate = (passed: number, total: number, low: number | null, high: number | null): Rate => ({
  passed,
  total,
  rate: total > 0 ? passed / total : null,
  interval_low: low,
  interval_high: high,
});

const summary: CallerRunSummary = {
  id: RUN_ID,
  run_id: "run-2026-11-25-a",
  workspace_id: "33333333-3333-3333-3333-333333333333",
  workspace_name: "Clinic tests",
  target_label: "Receptionist, test copy",
  rig_version: "0.1.0",
  scenario_set_hash: "abc",
  payload_version: 1,
  started_at: "2026-11-25T09:00:00Z",
  ended_at: "2026-11-25T09:20:00Z",
  uploaded_at: "2026-11-25T09:21:00Z",
  attempted: 4,
  graded: 2,
  passed: 1,
  failed: 1,
  not_graded: 2,
  not_run: 1,
  excluded_count: 3,
  outcome_counts: {
    passed: 1,
    failed: 1,
    void: 1,
    ungradeable: 1,
    synthetic_calls: 1,
    labels_unverified_calls: 2,
    unavailable_calls: 1,
  },
  rehearsal: false,
  of_graded: rate(1, 2, 0.09, 0.91),
  of_attempted: rate(1, 4, 0.05, 0.7),
};

const detail: CallerRunDetail = {
  ...summary,
  scenarios: [
    {
      scenario: "existing-patient",
      attempted: 1,
      graded: 1,
      failed: 0,
      not_graded: 0,
      ...rate(1, 1, 0.21, 1),
    },
    {
      scenario: "no-trace",
      attempted: 1,
      graded: 0,
      failed: 0,
      not_graded: 1,
      ...rate(0, 0, null, null),
    },
  ],
  failures: [
    {
      fault_class: "agent",
      calls: [
        {
          id: CALL_ID,
          position: 2,
          scenario: "misheard-date",
          last_step: "tool identify_patient refused at step VERIFY",
          failed_checks: ["the patient was found"],
        },
      ],
    },
  ],
  excluded: [
    {
      kind: "not graded",
      call_id: "44444444-4444-4444-4444-444444444444",
      scenario: "no-trace",
      scenario_file: "no-trace.json",
      repeat: 1,
      outcome: "void",
      fault: "rig",
      reason: "no trace was read for this call",
    },
    {
      kind: "not run",
      call_id: null,
      scenario: "books-an-appointment",
      scenario_file: "books.json",
      repeat: 1,
      outcome: "not run",
      reason: "mutation guard: scenario books, batch allows none",
    },
  ],
  chains: [],
  fields: [
    {
      field: "date_of_birth",
      answered: 4,
      stored_correctly: 1,
      sent_correctly: 2,
      misheard: 1,
      mis_stored: 1,
      not_stored: 0,
      read_back: 1,
      labels_unverified: 2,
      synthetic: 1,
    },
  ],
  calls: [
    {
      id: CALL_ID,
      position: 2,
      scenario: "misheard-date",
      outcome: "failed",
      fault_class: "agent",
      last_step: "tool identify_patient refused at step VERIFY",
      duration_s: 41.5,
      synthetic: true,
      labels_unverified: false,
    },
  ],
  reported_summary: null,
};

const call: CallerCallDetail = {
  id: CALL_ID,
  run: { id: RUN_ID, run_id: "run-2026-11-25-a", target_label: "Receptionist, test copy" },
  position: 2,
  file: "run-002.json",
  scenario: "misheard-date",
  scenario_file: "misheard-date.json",
  mutates: "none",
  repeat: 1,
  outcome: "failed",
  fault_class: "agent",
  last_step: "tool identify_patient refused at step VERIFY",
  conversation_id: "conv_123",
  ended: "agent ended the call",
  duration_s: 41.5,
  excluded_reason: null,
  fields: [
    {
      field: "date_of_birth",
      said: "5th July 1987",
      heard: "6th July 1987",
      stored: "1987-07-06",
      heard_matches: false,
      stored_matches: false,
      verdict: "misheard, stored wrongly",
      stored_source: "tool call",
    },
  ],
  checks: [{ what: "the patient was found", passed: false }],
  timeline: [
    { t: 0.4, kind: "agent", text: "Can I take your date of birth?" },
    { t: 2.1, kind: "said", clips: ["dob.wav"] },
    { t: 4.2, kind: "heard", text: "6th July 1987" },
    { t: 9, kind: "something_new", note: "a kind this page has not seen" },
  ],
  tool_calls: [
    {
      name: "identify_patient",
      arguments: { dateOfBirth: "1987-07-06" },
      result: { status: "not_found" },
      outputs: { status: "near_match" },
      ok: false,
      step_id: "VERIFY",
      t: 5,
      error: "",
    },
  ],
  said: [{ t: 2.1, field: "date_of_birth", intended: "5th July 1987", clips: ["dob.wav"] }],
  step_path: ["GREETING", "VERIFY"],
  read_back: null,
  synthetic_clips: [],
  labels_unverified: [],
  notes: {},
};

beforeEach(() => {
  route.params = {};
});

describe("caller runs list", () => {
  it("lists each run with its pass rate, interval and a link to it", async () => {
    server.use(
      http.get(API, () =>
        HttpResponse.json({ runs: [summary], total: 1, page: 1, page_size: 20, total_pages: 1 })
      )
    );

    render(<CallerRunsPage />);

    const link = await screen.findByRole("link", { name: "Receptionist, test copy" });
    expect(link).toHaveAttribute("href", `/dashboard/caller-runs/${RUN_ID}`);
    expect(screen.getByText("1 of 2 (50%)")).toBeInTheDocument();
    expect(screen.getByText("95% interval 9% to 91%")).toBeInTheDocument();
    expect(screen.getByText("Clinic tests")).toBeInTheDocument();
    expect(screen.getByText("1 run uploaded")).toBeInTheDocument();
  });

  it("marks a rehearsal run in the list and shows both rates with counts", async () => {
    const rehearsal = {
      ...summary,
      rehearsal: true,
      target_label: "REHEARSAL (scripted agent): Receptionist",
    };
    server.use(
      http.get(API, () =>
        HttpResponse.json({
          runs: [rehearsal, { ...summary, id: "z", run_id: "real" }],
          total: 2,
          page: 1,
          page_size: 20,
          total_pages: 1,
        })
      )
    );

    render(<CallerRunsPage />);

    await screen.findByRole("link", { name: /REHEARSAL \(scripted agent\): Receptionist/ });
    // One mark: on the rehearsal row, not the real one. (The label also says it in words.)
    expect(screen.getAllByText("REHEARSAL (scripted agent)")).toHaveLength(1);
    expect(screen.getAllByText("1 of 4 (25%)")).toHaveLength(2);
    expect(screen.getAllByText("1 of 2 (50%)")).toHaveLength(2);
    expect(screen.getAllByText("synthetic voice in 1 of 4 calls")).toHaveLength(2);
    expect(screen.getAllByText("labels unverified in 2 of 4 calls")).toHaveLength(2);
  });

  it("says a run with nothing graded has no pass rate, rather than showing 0%", async () => {
    const ungraded = { ...summary, graded: 0, passed: 0, of_graded: rate(0, 0, null, null) };
    server.use(
      http.get(API, () =>
        HttpResponse.json({ runs: [ungraded], total: 1, page: 1, page_size: 20, total_pages: 1 })
      )
    );

    render(<CallerRunsPage />);

    expect(await screen.findByText("none graded")).toBeInTheDocument();
    expect(screen.queryByText(/0%/)).not.toBeInTheDocument();
  });

  it("explains how to upload when there are no runs", async () => {
    server.use(
      http.get(API, () =>
        HttpResponse.json({ runs: [], total: 0, page: 1, page_size: 20, total_pages: 0 })
      )
    );

    render(<CallerRunsPage />);

    expect(await screen.findByText("No caller runs yet")).toBeInTheDocument();
    expect(screen.getByText(/synthiq-caller upload/)).toBeInTheDocument();
  });

  it("shows an error when the list cannot be loaded", async () => {
    server.use(http.get(API, () => HttpResponse.json({ detail: "boom" }, { status: 500 })));

    render(<CallerRunsPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load caller runs");
  });
});

describe("caller run page", () => {
  beforeEach(() => {
    route.params = { runId: RUN_ID };
  });

  it("shows every outcome class on its own line, never merged", async () => {
    const five = {
      ...detail,
      attempted: 5,
      not_graded: 3,
      outcome_counts: { ...detail.outcome_counts, ungraded: 1 },
    };
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(five)));

    render(<CallerRunPage />);

    await screen.findByRole("heading", { name: "Receptionist, test copy" });
    const line = (label: string) => screen.getByText(label).nextElementSibling;
    expect(line("Void (rig could not answer)")).toHaveTextContent("1");
    expect(line("Ungradeable")).toHaveTextContent("1");
    expect(line("Ungraded")).toHaveTextContent("1");
    expect(line("Other, not graded")).toHaveTextContent("0");
    expect(line("Unavailable (tool result withheld)")).toHaveTextContent("1");
    expect(line("Not run")).toHaveTextContent("1");
  });

  it("marks a rehearsal run, and does not mark a real one", async () => {
    const rehearsal = {
      ...detail,
      rehearsal: true,
      outcome_counts: { ...detail.outcome_counts, rehearsal_calls: 4 },
    };
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(rehearsal)));

    const { unmount } = render(<CallerRunPage />);

    const note = await screen.findByRole("note", { name: "Rehearsal" });
    expect(note).toHaveTextContent("REHEARSAL (scripted agent)");
    expect(note).toHaveTextContent("4 of 4 calls");
    unmount();

    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(detail)));
    render(<CallerRunPage />);
    await screen.findByRole("heading", { name: "Receptionist, test copy" });
    expect(screen.queryByText(/REHEARSAL/)).not.toBeInTheDocument();
  });

  it("never rounds a rate up: 199 of 200 is not 100%", async () => {
    const near = {
      ...detail,
      scenarios: [],
      graded: 200,
      passed: 199,
      of_graded: rate(199, 200, 0.972, 0.9998),
    };
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(near)));

    render(<CallerRunPage />);

    expect(await screen.findByText("199 of 200 (99.5%)")).toBeInTheDocument();
    expect(screen.queryByText(/100%/)).not.toBeInTheDocument();
  });

  it("shows both pass rates and every count", async () => {
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(detail)));

    render(<CallerRunPage />);

    expect(
      await screen.findByRole("heading", { name: "Receptionist, test copy" })
    ).toBeInTheDocument();
    expect(screen.getByText("1 of 2 (50%)")).toBeInTheDocument();
    expect(screen.getByText("95% interval 9% to 91%")).toBeInTheDocument();
    expect(screen.getByText("1 of 4 (25%)")).toBeInTheDocument();
  });

  it("gives a scenario with nothing graded no rate", async () => {
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(detail)));

    render(<CallerRunPage />);

    const table = await screen.findByRole("table", { name: "Pass rate for each scenario" });
    const row = within(table).getByText("no-trace").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("none graded")).toBeInTheDocument();
    expect(within(table).getByText("1 of 1 (100%)")).toBeInTheDocument();
  });

  it("groups failures by fault class and links each to its call", async () => {
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(detail)));

    render(<CallerRunPage />);

    const group = await screen.findByRole("region", { name: "Fault class agent" });
    const link = within(group).getByRole("link", { name: "Call 2: misheard-date" });
    expect(link).toHaveAttribute("href", `/dashboard/caller-runs/${RUN_ID}/calls/${CALL_ID}`);
    expect(
      within(group).getByText("Stopped at: tool identify_patient refused at step VERIFY")
    ).toBeInTheDocument();
    expect(within(group).getByText("Failed check: the patient was found")).toBeInTheDocument();
  });

  it("lists every call left out of the pass rate with its reason", async () => {
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(detail)));

    render(<CallerRunPage />);

    const table = await screen.findByRole("table", {
      name: "Calls that are not counted in the pass rate, with the reason for each",
    });
    expect(within(table).getByText("no trace was read for this call")).toBeInTheDocument();
    expect(
      within(table).getByText("mutation guard: scenario books, batch allows none")
    ).toBeInTheDocument();
    // A call that was placed links to its page; one that never ran has no page to link to.
    expect(within(table).getByRole("link", { name: "no-trace" })).toBeInTheDocument();
    expect(
      within(table).queryByRole("link", { name: "books-an-appointment" })
    ).not.toBeInTheDocument();
  });

  it("counts void and ungradeable calls apart and names the fault of a void call", async () => {
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(detail)));

    render(<CallerRunPage />);

    const void_ = await screen.findByText("Void (rig could not answer)");
    expect(void_.nextElementSibling).toHaveTextContent("1");
    expect(screen.getByText("Ungradeable").nextElementSibling).toHaveTextContent("1");
    // Two not graded in all, one void and one ungradeable, so none unexplained.
    expect(screen.getByText("Other, not graded").nextElementSibling).toHaveTextContent("0");
    expect(screen.getByText("fault: rig")).toBeInTheDocument();
  });

  it("says when a synthetic voice or an unverified label weakens the result", async () => {
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(detail)));

    render(<CallerRunPage />);

    const note = await screen.findByRole("note", { name: "Clip provenance" });
    expect(note).toHaveTextContent("1 of 4 calls used a synthetic voice");
    expect(note).toHaveTextContent("2 of 4 calls played a clip with an unverified label");
  });

  it("keeps sent-but-not-confirmed apart from stored correctly", async () => {
    server.use(http.get(`${API}/${RUN_ID}`, () => HttpResponse.json(detail)));

    render(<CallerRunPage />);

    const table = await screen.findByRole("table", {
      name: /how often it was stored correctly, only sent, misheard/,
    });
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent);
    const cells = within(within(table).getByText("date_of_birth").closest("tr") as HTMLElement)
      .getAllByRole("cell")
      .map((cell) => cell.textContent);
    expect(cells[headers.indexOf("Stored correctly")]).toBe("1");
    expect(cells[headers.indexOf("Sent, not confirmed stored")]).toBe("2");
    expect(cells[headers.indexOf("Labels unverified")]).toBe("2");
    expect(cells[headers.indexOf("Synthetic voice")]).toBe("1");
  });

  it("shows an error for a run that cannot be read", async () => {
    server.use(
      http.get(`${API}/${RUN_ID}`, () =>
        HttpResponse.json({ detail: "Caller run not found" }, { status: 404 })
      )
    );

    render(<CallerRunPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Failed to load this run");
    expect(alert).toHaveTextContent("belong to another account");
  });
});

describe("caller call page", () => {
  beforeEach(() => {
    route.params = { runId: RUN_ID, callId: CALL_ID };
  });

  it("says a tool call whose result was withheld is unavailable, not refused", async () => {
    const withheld = {
      ...call,
      tool_calls: [
        {
          name: "book_slot",
          arguments: { _unavailable: "the platform returned no result" },
          result: "unavailable",
          ok: false,
          t: 6,
          error: "unavailable: the platform returned no result",
        },
      ],
    };
    server.use(http.get(`${API}/${RUN_ID}/calls/${CALL_ID}`, () => HttpResponse.json(withheld)));

    render(<CallerCallPage />);

    expect(await screen.findByText("Tool call, result unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Tool call refused")).not.toBeInTheDocument();
  });

  it("shows said, heard and stored for each field, and where stored came from", async () => {
    server.use(http.get(`${API}/${RUN_ID}/calls/${CALL_ID}`, () => HttpResponse.json(call)));

    render(<CallerCallPage />);

    const table = await screen.findByRole("table");
    const row = within(table).getByText("date_of_birth").closest("tr") as HTMLElement;
    expect(within(row).getByText("5th July 1987")).toBeInTheDocument();
    expect(within(row).getByText("6th July 1987")).toBeInTheDocument();
    expect(within(row).getByText("1987-07-06")).toBeInTheDocument();
    expect(within(row).getByText("misheard")).toBeInTheDocument();
    expect(within(row).getByText("tool call")).toBeInTheDocument();
    expect(screen.getByText(/not a record read back from the system/)).toBeInTheDocument();
  });

  it("lays the call out in time order, tool calls included", async () => {
    server.use(http.get(`${API}/${RUN_ID}/calls/${CALL_ID}`, () => HttpResponse.json(call)));

    render(<CallerCallPage />);

    const list = await screen.findByRole("list", { name: "Call timeline" });
    const timeline = within(list)
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "");
    expect(timeline).toHaveLength(5);
    expect(timeline[0]).toContain("Agent said");
    expect(timeline[0]).toContain("Can I take your date of birth?");
    // The caller's line is what the clip was meant to say, with the clip named.
    expect(timeline[1]).toContain("Caller said");
    expect(timeline[1]).toContain("5th July 1987");
    expect(timeline[1]).toContain("dob.wav");
    expect(timeline[2]).toContain("Agent heard");
    expect(timeline[3]).toContain("Tool call refused");
    expect(timeline[3]).toContain("identify_patient");
    // What the tool reported back is shown, so found and near match can be told apart.
    expect(timeline[3]).toContain("near_match");
    // An event kind this page does not know is shown, not dropped.
    expect(timeline[4]).toContain("something_new");
  });

  it("says why a call is not counted when it was left out", async () => {
    const leftOut: CallerCallDetail = {
      ...call,
      outcome: "ungraded",
      excluded_reason: "no trace was read for this call",
      fields: [],
      checks: [],
    };
    server.use(http.get(`${API}/${RUN_ID}/calls/${CALL_ID}`, () => HttpResponse.json(leftOut)));

    render(<CallerCallPage />);

    expect(
      await screen.findByText("This call is not counted in the pass rate.")
    ).toBeInTheDocument();
    expect(screen.getByText("no trace was read for this call")).toBeInTheDocument();
    expect(screen.getByText("The caller gave no graded detail in this call.")).toBeInTheDocument();
  });

  it("marks synthetic clips and unverified labels on the call and its fields", async () => {
    const marked: CallerCallDetail = {
      ...call,
      synthetic_clips: ["synthetic/dob.wav"],
      labels_unverified: ["dob.wav"],
      fields: [
        { ...call.fields[0], field: "date_of_birth", synthetic: true, labels_unverified: true },
      ],
    };
    server.use(http.get(`${API}/${RUN_ID}/calls/${CALL_ID}`, () => HttpResponse.json(marked)));

    render(<CallerCallPage />);

    const note = await screen.findByRole("note", { name: "Clip provenance" });
    expect(note).toHaveTextContent("synthetic/dob.wav");
    expect(note).toHaveTextContent("no person has confirmed what these clips say: dob.wav");
    const row = within(screen.getByRole("table")).getByText("date_of_birth").closest("tr");
    expect(within(row as HTMLElement).getByText("synthetic voice")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("label unverified")).toBeInTheDocument();
  });

  it("shows an error for a call that cannot be read", async () => {
    server.use(
      http.get(`${API}/${RUN_ID}/calls/${CALL_ID}`, () =>
        HttpResponse.json({ detail: "Caller call not found" }, { status: 404 })
      )
    );

    render(<CallerCallPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load this call");
  });
});
