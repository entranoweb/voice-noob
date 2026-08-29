/**
 * Tests for ScenarioManager component (Task 5.1)
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { server } from "@/test/test-utils";
import { ScenarioManager } from "../scenario-manager";

const API_URL = "http://localhost:8000";

// Mock sonner toast
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

// Wrap with QueryClientProvider for tests
const renderWithProviders = (component: React.ReactNode, queryClient?: QueryClient) => {
  const client =
    queryClient ??
    new QueryClient({
      defaultOptions: {
        queries: {
          retry: false,
          gcTime: 0,
          staleTime: 0,
        },
      },
    });
  return render(<QueryClientProvider client={client}>{component}</QueryClientProvider>);
};

describe("ScenarioManager", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders without crashing", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByText("Test Scenarios")).toBeInTheDocument();
    });
  });

  it("displays header with title and description", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByText("Test Scenarios")).toBeInTheDocument();
      expect(
        screen.getByText("Create and manage test scenarios for your voice agents")
      ).toBeInTheDocument();
    });
  });

  it("displays Seed Built-in button", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /seed built-in/i })).toBeInTheDocument();
    });
  });

  it("displays search input", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText("Search scenarios...")).toBeInTheDocument();
    });
  });

  it("displays filter dropdowns", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      // Check for filter controls
      expect(screen.getByText("All Types")).toBeInTheDocument();
      expect(screen.getByText("All Categories")).toBeInTheDocument();
      expect(screen.getByText("All Difficulties")).toBeInTheDocument();
    });
  });

  it("displays scenarios grouped by category", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      // Check for category groups from mock data
      expect(screen.getByText("greeting")).toBeInTheDocument();
      expect(screen.getByText("booking")).toBeInTheDocument();
      expect(screen.getByText("custom")).toBeInTheDocument();
    });
  });

  it("displays scenario names", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
      expect(screen.getByText("Appointment Booking")).toBeInTheDocument();
      expect(screen.getByText("Custom VIP Scenario")).toBeInTheDocument();
    });
  });

  it("displays built-in badge for built-in scenarios", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      const badges = screen.getAllByText("Built-in");
      expect(badges.length).toBeGreaterThan(0);
    });
  });

  it("displays difficulty badges", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByText("easy")).toBeInTheDocument();
      expect(screen.getByText("medium")).toBeInTheDocument();
      expect(screen.getByText("hard")).toBeInTheDocument();
    });
  });

  it("shows empty state when no scenarios exist", async () => {
    server.use(
      http.get(`${API_URL}/api/v1/testing/scenarios`, () => {
        return HttpResponse.json([]);
      })
    );

    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByText("No test scenarios yet")).toBeInTheDocument();
      expect(screen.getByText("Seed built-in scenarios or create your own")).toBeInTheDocument();
    });
  });

  it("shows error state when scenarios fail to load", async () => {
    server.use(
      http.get(`${API_URL}/api/v1/testing/scenarios`, () => {
        return HttpResponse.json({ detail: "Failed to load scenarios" }, { status: 500 });
      })
    );

    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      // Use getAllByText since there are multiple elements with this text
      const errorMessages = screen.getAllByText("Failed to load scenarios");
      expect(errorMessages.length).toBeGreaterThan(0);
    });
  });

  it("filters scenarios by search query", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
    });

    // Type in search box
    const searchInput = screen.getByPlaceholderText("Search scenarios...");
    await user.type(searchInput, "VIP");

    // Should only show VIP scenario
    await waitFor(() => {
      expect(screen.getByText("Custom VIP Scenario")).toBeInTheDocument();
      expect(screen.queryByText("Basic Greeting Test")).not.toBeInTheDocument();
    });
  });

  it("shows no matching scenarios message when search returns empty", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
    });

    // Type in search box with no matches
    const searchInput = screen.getByPlaceholderText("Search scenarios...");
    await user.type(searchInput, "nonexistent");

    await waitFor(() => {
      expect(screen.getByText("No matching scenarios")).toBeInTheDocument();
    });
  });

  it("calls onCreateScenario when Create Scenario button is clicked", async () => {
    const user = userEvent.setup();
    const onCreateScenario = vi.fn();

    renderWithProviders(<ScenarioManager onCreateScenario={onCreateScenario} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /create scenario/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /create scenario/i }));

    expect(onCreateScenario).toHaveBeenCalledTimes(1);
  });

  it("displays loading skeleton while fetching scenarios", () => {
    server.use(
      http.get(`${API_URL}/api/v1/testing/scenarios`, async () => {
        await new Promise((resolve) => setTimeout(resolve, 100));
        return HttpResponse.json([]);
      })
    );

    renderWithProviders(<ScenarioManager />);

    // Should show skeleton loading states
    const skeletons = document.querySelectorAll('[class*="animate-pulse"]');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("handles scenario selection when onSelectionChange is provided", async () => {
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();

    renderWithProviders(
      <ScenarioManager selectedScenarioIds={[]} onSelectionChange={onSelectionChange} />
    );

    await waitFor(() => {
      expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
    });

    // Find and click a checkbox
    const checkboxes = screen.getAllByRole("checkbox");
    const firstCheckbox = checkboxes[0];
    if (!firstCheckbox) throw new Error("No checkbox found");
    await user.click(firstCheckbox);

    expect(onSelectionChange).toHaveBeenCalled();
  });

  it("shows selection count when scenarios are selected", async () => {
    renderWithProviders(
      <ScenarioManager
        selectedScenarioIds={["scenario-1", "scenario-2"]}
        onSelectionChange={vi.fn()}
      />
    );

    // Wait for scenarios to load first
    await waitFor(() => {
      expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
    });

    // The selection count should appear in the fixed bottom bar
    await waitFor(() => {
      // Look for the text that contains "2" and "selected"
      expect(screen.getByText("2")).toBeInTheDocument();
      expect(screen.getByText(/selected/i)).toBeInTheDocument();
    });
  });

  it("shows clear button when scenarios are selected", async () => {
    renderWithProviders(
      <ScenarioManager selectedScenarioIds={["scenario-1"]} onSelectionChange={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /clear/i })).toBeInTheDocument();
    });
  });

  it("displays category count badges", async () => {
    renderWithProviders(<ScenarioManager />);

    await waitFor(() => {
      // Each category should show a count badge
      const greetingSection = screen.getByText("greeting").closest("div");
      expect(greetingSection).toBeInTheDocument();
    });
  });
});
