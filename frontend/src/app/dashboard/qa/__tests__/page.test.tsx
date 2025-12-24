/**
 * Tests for QA Dashboard Page (Task 20.5.5)
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import QADashboardPage from "../page";

// Mock next/navigation
const mockPush = () => {};
const mockReplace = () => {};
const mockPrefetch = () => {};

// Manual mock setup before imports
globalThis.mockUseRouter = {
  push: mockPush,
  replace: mockReplace,
  prefetch: mockPrefetch,
};

// Import mocks
import { http, HttpResponse } from "msw";
import { server } from "@/test/test-utils";

// Mock useAuth hook
const mockUser = { id: 1, email: "test@example.com" };

// Setup module mocks
import * as useAuthModule from "@/hooks/use-auth";
import * as navigationModule from "next/navigation";

// Mock the modules
vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({
    user: mockUser,
    isLoading: false,
  }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => globalThis.mockUseRouter,
  useSearchParams: () => new URLSearchParams(),
}));

// Wrap with QueryClientProvider for tests
const renderWithProviders = (component: React.ReactNode) => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
        staleTime: 0,
      },
    },
  });
  return render(<QueryClientProvider client={queryClient}>{component}</QueryClientProvider>);
};

describe("QA Dashboard Page", () => {
  beforeEach(() => {
    // Reset any request handlers that might have been added
    server.resetHandlers();

    // Set up localStorage for auth
    if (typeof window !== "undefined") {
      localStorage.setItem("access_token", "test-token");
    }
  });

  afterEach(() => {
    // Clean up
    if (typeof window !== "undefined") {
      localStorage.clear();
    }
  });

  it("renders without crashing", async () => {
    renderWithProviders(<QADashboardPage />);

    // Wait for the page to load - look for the heading
    await waitFor(
      () => {
        const heading = screen.getByText("QA Dashboard");
        expect(heading).toBeInTheDocument();
      },
      { timeout: 5000 }
    );
  });

  it("displays QA Dashboard title or heading", async () => {
    renderWithProviders(<QADashboardPage />);

    // The heading should appear
    const heading = await screen.findByText("QA Dashboard", {}, { timeout: 5000 });
    expect(heading).toBeInTheDocument();
  });

  it("renders metrics cards after loading", async () => {
    renderWithProviders(<QADashboardPage />);

    // Wait for metrics to load - look for Pass Rate card
    await waitFor(
      async () => {
        const passRateText = await screen.findByText("Pass Rate");
        expect(passRateText).toBeInTheDocument();
      },
      { timeout: 5000 }
    );

    // Verify other metrics are present
    expect(screen.getByText("Average Score")).toBeInTheDocument();
    expect(screen.getByText("Total Evaluations")).toBeInTheDocument();
    expect(screen.getByText("Failed Calls")).toBeInTheDocument();
  });

  it("renders failure reasons section", async () => {
    renderWithProviders(<QADashboardPage />);

    // Wait for failure reasons to load
    await waitFor(
      async () => {
        const failureReasonsTitle = await screen.findByText("Top Failure Reasons");
        expect(failureReasonsTitle).toBeInTheDocument();
      },
      { timeout: 5000 }
    );

    // Check for at least one failure reason from mock data
    await waitFor(
      () => {
        const intentText = screen.queryByText(/Intent not completed/i);
        expect(intentText).toBeInTheDocument();
      },
      { timeout: 2000 }
    );
  });

  it("shows QA disabled message when QA is disabled", async () => {
    // Override the QA status handler to return disabled
    server.use(
      http.get("http://localhost:8000/api/v1/qa/status", () => {
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

    // Should show the disabled message
    const disabledMessage = await screen.findByText(
      "QA Testing Disabled",
      {},
      { timeout: 5000 }
    );
    expect(disabledMessage).toBeInTheDocument();
  });

  it("displays score breakdown section", async () => {
    renderWithProviders(<QADashboardPage />);

    // Wait for score breakdown section
    await waitFor(
      async () => {
        const scoreBreakdownTitle = await screen.findByText("Score Breakdown");
        expect(scoreBreakdownTitle).toBeInTheDocument();
      },
      { timeout: 5000 }
    );

    // Check for score breakdown items
    expect(screen.getByText("Intent Completion")).toBeInTheDocument();
    expect(screen.getByText("Tool Usage")).toBeInTheDocument();
    expect(screen.getByText("Compliance")).toBeInTheDocument();
    expect(screen.getByText("Response Quality")).toBeInTheDocument();
  });
});
