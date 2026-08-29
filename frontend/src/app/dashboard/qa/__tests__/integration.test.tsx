/**
 * Integration Tests for QA Dashboard (Task 5.2)
 * Tests full flows: create scenario → run test → view results
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { server } from "@/test/test-utils";

// Mock next/navigation
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
  }),
  useSearchParams: () => new URLSearchParams(),
}));

// Mock useAuth hook
vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({
    user: { id: 1, email: "test@example.com" },
    isLoading: false,
  }),
}));

// Mock sonner toast
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import QADashboardPage from "../page";

const API_URL = "http://localhost:8000";

const renderWithProviders = (component: React.ReactNode) => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
    },
  });
  return render(<QueryClientProvider client={queryClient}>{component}</QueryClientProvider>);
};

describe("QA Dashboard Integration Tests", () => {
  beforeEach(() => {
    server.resetHandlers();
    if (typeof window !== "undefined") {
      localStorage.setItem("access_token", "test-token");
    }
  });

  afterEach(() => {
    vi.clearAllMocks();
    if (typeof window !== "undefined") {
      localStorage.clear();
    }
  });

  describe("Tab Navigation", () => {
    it("switches between Overview, Scenarios, and Settings tabs", async () => {
      const user = userEvent.setup();

      // Add workspace handler for this test
      server.use(
        http.get(`${API_URL}/api/v1/workspaces`, () => {
          return HttpResponse.json([
            { id: "ws-1", name: "Test Workspace", description: null, is_default: true },
          ]);
        })
      );

      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      // Click Scenarios tab
      const scenariosTab = screen.getByRole("tab", { name: /scenarios/i });
      await user.click(scenariosTab);

      await waitFor(
        () => {
          expect(screen.getByText("Test Scenarios")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      // Click Settings tab
      const settingsTab = screen.getByRole("tab", { name: /settings/i });
      await user.click(settingsTab);

      await waitFor(
        () => {
          // Settings tab shows Workspace Override section
          expect(screen.getByText("Workspace Override")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });
  });

  describe("Test Runner Flow", () => {
    it("opens test runner when Run Tests button is clicked", async () => {
      const user = userEvent.setup();
      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const runTestsButton = screen.getByRole("button", { name: /run tests/i });
      await user.click(runTestsButton);

      await waitFor(
        () => {
          expect(screen.getByRole("dialog")).toBeInTheDocument();
          expect(screen.getByText("Select Agent")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });

    it("displays scenarios in test runner", async () => {
      const user = userEvent.setup();
      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const runTestsButton = screen.getByRole("button", { name: /run tests/i });
      await user.click(runTestsButton);

      await waitFor(
        () => {
          expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });
  });

  describe("Scenario Management Flow", () => {
    it("displays scenarios in Scenarios tab", async () => {
      const user = userEvent.setup();
      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const scenariosTab = screen.getByRole("tab", { name: /scenarios/i });
      await user.click(scenariosTab);

      await waitFor(
        () => {
          expect(screen.getByText("Basic Greeting Test")).toBeInTheDocument();
          expect(screen.getByText("Appointment Booking")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });

    it("opens create scenario dialog", async () => {
      const user = userEvent.setup();
      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const scenariosTab = screen.getByRole("tab", { name: /scenarios/i });
      await user.click(scenariosTab);

      await waitFor(
        () => {
          expect(screen.getByRole("button", { name: /create scenario/i })).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const createButton = screen.getByRole("button", { name: /create scenario/i });
      await user.click(createButton);

      await waitFor(
        () => {
          const dialogs = screen.getAllByRole("dialog");
          expect(dialogs.length).toBeGreaterThan(0);
        },
        { timeout: 5000 }
      );
    });
  });

  describe("Workspace Settings Override", () => {
    it("displays workspace settings in Settings tab", async () => {
      const user = userEvent.setup();

      // Add workspace handler
      server.use(
        http.get(`${API_URL}/api/v1/workspaces`, () => {
          return HttpResponse.json([
            { id: "ws-1", name: "Test Workspace", description: null, is_default: true },
          ]);
        })
      );

      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const settingsTab = screen.getByRole("tab", { name: /settings/i });
      await user.click(settingsTab);

      await waitFor(
        () => {
          // Settings panel shows "Evaluation Settings" card
          expect(screen.getByText("Evaluation Settings")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });

    it("shows workspace override section", async () => {
      const user = userEvent.setup();

      server.use(
        http.get(`${API_URL}/api/v1/workspaces`, () => {
          return HttpResponse.json([
            { id: "ws-1", name: "Test Workspace", description: null, is_default: true },
          ]);
        })
      );

      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const settingsTab = screen.getByRole("tab", { name: /settings/i });
      await user.click(settingsTab);

      await waitFor(
        () => {
          // The actual text is "Use workspace-specific settings"
          expect(screen.getByText("Use workspace-specific settings")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });
  });

  describe("Error States", () => {
    it("shows QA disabled message when QA is disabled", async () => {
      server.use(
        http.get(`${API_URL}/api/v1/qa/status`, () => {
          return HttpResponse.json({
            enabled: false,
            auto_evaluate: false,
            evaluation_model: "claude-sonnet-4-20250514",
            default_threshold: 70,
            api_key_configured: false,
          });
        })
      );

      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Testing Disabled")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });

    it("handles API error gracefully for metrics", async () => {
      server.use(
        http.get(`${API_URL}/api/v1/qa/dashboard/metrics`, () => {
          return HttpResponse.json({ detail: "Server error" }, { status: 500 });
        })
      );

      renderWithProviders(<QADashboardPage />);

      // Should still render the page structure
      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });

    it("handles empty scenarios list", async () => {
      const user = userEvent.setup();

      server.use(
        http.get(`${API_URL}/api/v1/testing/scenarios`, () => {
          return HttpResponse.json([]);
        })
      );

      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const scenariosTab = screen.getByRole("tab", { name: /scenarios/i });
      await user.click(scenariosTab);

      await waitFor(
        () => {
          // The actual text is "No test scenarios yet"
          expect(screen.getByText("No test scenarios yet")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });

    it("handles empty evaluations list", async () => {
      server.use(
        http.get(`${API_URL}/api/v1/qa/evaluations`, () => {
          return HttpResponse.json({
            evaluations: [],
            total: 0,
            page: 1,
            page_size: 20,
            total_pages: 0,
          });
        })
      );

      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("No evaluations yet")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    });
  });

  describe("Filter Controls", () => {
    it("displays time range filter", async () => {
      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      expect(screen.getByText("Last 7 days")).toBeInTheDocument();
    });

    it("displays agent filter", async () => {
      renderWithProviders(<QADashboardPage />);

      await waitFor(
        () => {
          expect(screen.getByText("QA Dashboard")).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      expect(screen.getByText("All agents")).toBeInTheDocument();
    });
  });
});
