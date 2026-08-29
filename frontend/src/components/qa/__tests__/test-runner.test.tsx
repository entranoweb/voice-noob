/**
 * Tests for TestRunner component (Task 5.1)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { server } from "@/test/test-utils";
import { TestRunner } from "../test-runner";

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
});
