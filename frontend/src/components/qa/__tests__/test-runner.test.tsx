/**
 * Tests for TestRunner component (Task 5.1)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { server } from "@/test/test-utils";
import { TestRunner, TEXT_HARNESS_NOTICE } from "../test-runner";

// Radix Select opens a portal that happy-dom cannot drive with pointer events,
// so the agent picker is replaced by a native <select> with the same contract
// (value, onValueChange, disabled). Everything else on the sheet is the real thing.
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const collectItems = (node: React.ReactNode): React.ReactElement[] =>
    React.Children.toArray(node).flatMap((child) => {
      if (!React.isValidElement<{ value?: string; children?: React.ReactNode }>(child)) return [];
      if (child.props.value !== undefined) return [child];
      return collectItems(child.props.children);
    });
  return {
    Select: ({
      value,
      onValueChange,
      disabled,
      children,
    }: {
      value?: string;
      onValueChange?: (value: string) => void;
      disabled?: boolean;
      children?: React.ReactNode;
    }) => (
      <select
        aria-label="Agent"
        value={value}
        disabled={disabled}
        onChange={(event) => onValueChange?.(event.target.value)}
      >
        <option value="">Choose an agent to test</option>
        {collectItems(children).map((item) => {
          const props = item.props as { value: string; children?: React.ReactNode };
          return (
            <option key={props.value} value={props.value}>
              {props.children}
            </option>
          );
        })}
      </select>
    ),
    SelectContent: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    SelectItem: ({ children }: { value: string; children?: React.ReactNode }) => <>{children}</>,
    SelectTrigger: () => null,
    SelectValue: () => null,
  };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const renderWithProviders = (component: React.ReactNode) => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(<QueryClientProvider client={queryClient}>{component}</QueryClientProvider>);
};

describe("TestRunner", () => {
  beforeEach(() => {
    server.resetHandlers();
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("does not render sheet when open is false", () => {
    renderWithProviders(<TestRunner open={false} onOpenChange={vi.fn()} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders sheet when open is true", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
  });

  it("displays Run Tests title", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("Run Tests")).toBeInTheDocument();
    });
  });

  it("displays agent selection dropdown", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("Select Agent")).toBeInTheDocument();
    });
  });

  it("displays Select Scenarios section", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("Select Scenarios")).toBeInTheDocument();
    });
  });

  it("displays Select All button", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /select all/i })).toBeInTheDocument();
    });
  });

  it("displays Run button", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      const runButton = screen.getByRole("button", { name: /run.*test/i });
      expect(runButton).toBeInTheDocument();
    });
  });

  it("Run button is disabled when no agent selected", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      const runButton = screen.getByRole("button", { name: /run.*test/i });
      expect(runButton).toBeDisabled();
    });
  });

  it("displays scenarios from API", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
      expect(screen.getByText("Appointment Booking")).toBeInTheDocument();
    });
  });

  it("displays scenario categories", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      // Category headers are rendered as h4 elements with uppercase text
      const greetingHeaders = screen.getAllByText(/greeting/i);
      const bookingHeaders = screen.getAllByText(/booking/i);
      expect(greetingHeaders.length).toBeGreaterThan(0);
      expect(bookingHeaders.length).toBeGreaterThan(0);
    });
  });

  it("displays Built-in badge for built-in scenarios", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      const badges = screen.getAllByText("Built-in");
      expect(badges.length).toBeGreaterThan(0);
    });
  });

  it("allows selecting scenarios via checkbox", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
    });

    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes.length).toBeGreaterThan(0);
    const firstCheckbox = checkboxes[0];
    if (!firstCheckbox) throw new Error("No checkbox found");
    await user.click(firstCheckbox);

    await waitFor(() => {
      expect(screen.getByText(/1 scenario.*selected/i)).toBeInTheDocument();
    });
  });

  it("updates selection count when scenarios are selected", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
    });

    const checkboxes = screen.getAllByRole("checkbox");
    const [firstCheckbox, secondCheckbox] = checkboxes;
    if (!firstCheckbox || !secondCheckbox) throw new Error("Expected at least 2 checkboxes");
    await user.click(firstCheckbox);
    await user.click(secondCheckbox);

    await waitFor(() => {
      expect(screen.getByText(/2 scenarios selected/i)).toBeInTheDocument();
    });
  });

  it("displays difficulty badges", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText(/easy/i)).toBeInTheDocument();
      expect(screen.getByText(/medium/i)).toBeInTheDocument();
    });
  });

  // ---------------------------------------------------------------------------
  // Running. These go through the routes the backend really serves:
  // POST /api/v1/testing/run (one scenario per request) and
  // GET /api/v1/testing/runs/{id}. The button used to post to a route that does
  // not exist, and a mock handler for that route kept the old tests green.
  // ---------------------------------------------------------------------------

  const chooseAgent = async (agentId: string) => {
    await waitFor(() => {
      expect(document.querySelector(`select option[value="${agentId}"]`)).not.toBeNull();
    });
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: agentId } });
  };

  const tickScenario = async (user: ReturnType<typeof userEvent.setup>, name: string) => {
    const label = (await screen.findByText(name)).closest("label");
    const checkbox = label?.querySelector('[role="checkbox"]');
    if (!checkbox) throw new Error(`No checkbox for ${name}`);
    await user.click(checkbox);
  };

  it("says what a text run does not cover", async () => {
    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    expect(await screen.findByTestId("text-harness-notice")).toHaveTextContent(TEXT_HARNESS_NOTICE);
    expect(TEXT_HARNESS_NOTICE).toMatch(/not the speech model that answers real calls/);
  });

  it("runs each selected scenario through POST /testing/run and shows its stored result", async () => {
    const user = userEvent.setup();
    const posted: Array<Record<string, unknown>> = [];
    server.events.on("request:start", ({ request }) => {
      if (request.method === "POST" && new URL(request.url).pathname === "/api/v1/testing/run") {
        void request
          .clone()
          .json()
          .then((body) => posted.push(body as Record<string, unknown>));
      }
    });
    const onTestComplete = vi.fn();

    renderWithProviders(
      <TestRunner
        open={true}
        onOpenChange={vi.fn()}
        workspaceId="ws-1"
        onTestComplete={onTestComplete}
      />
    );
    await chooseAgent("agent-1");
    await tickScenario(user, "Basic Greeting Test");
    await tickScenario(user, "Appointment Booking");
    await user.click(screen.getByRole("button", { name: /run 2 tests/i }));

    await waitFor(() => {
      expect(screen.getByText("1 passed")).toBeInTheDocument();
      expect(screen.getByText("1 failed")).toBeInTheDocument();
    });
    expect(screen.getByText("PASS")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText("2/2 Complete")).toBeInTheDocument();
    expect(onTestComplete).toHaveBeenCalledTimes(1);
    expect(posted).toEqual([
      { scenario_id: "scenario-1", agent_id: "agent-1", workspace_id: "ws-1" },
      { scenario_id: "scenario-2", agent_id: "agent-1", workspace_id: "ws-1" },
    ]);
    server.events.removeAllListeners();
  });

  it("stops and says why when the backend refuses the run", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    server.use(
      http.post("http://localhost:8000/api/v1/testing/run", () => {
        attempts += 1;
        return HttpResponse.json({ detail: "QA testing is disabled" }, { status: 400 });
      })
    );

    renderWithProviders(<TestRunner open={true} onOpenChange={vi.fn()} />);
    await chooseAgent("agent-1");
    await tickScenario(user, "Basic Greeting Test");
    await tickScenario(user, "Appointment Booking");
    await user.click(screen.getByRole("button", { name: /run 2 tests/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The run stopped: QA testing is disabled"
    );
    // The second scenario would be refused for the same reason, so it is not tried
    // and nothing is reported as a failure of the agent.
    expect(attempts).toBe(1);
    expect(screen.getByText("0 failed")).toBeInTheDocument();
    expect(screen.getByText("1 not run")).toBeInTheDocument();
  });
});
