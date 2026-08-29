/**
 * Tests for QASettingsPanel component (Task 5.1)
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { server } from "@/test/test-utils";
import { QASettingsPanel } from "../qa-settings-panel";

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

describe("QASettingsPanel", () => {
  const workspaceId = "workspace-1";

  beforeEach(() => {
    server.resetHandlers();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders without crashing", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("QA Status")).toBeInTheDocument();
    });
  });

  it("displays QA status card with enabled status", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("QA Status")).toBeInTheDocument();
      expect(screen.getByText("Enabled")).toBeInTheDocument();
    });
  });

  it("displays API key configured status", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("Configured")).toBeInTheDocument();
    });
  });

  it("displays workspace override section", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("Workspace Override")).toBeInTheDocument();
      expect(screen.getByText("Use workspace-specific settings")).toBeInTheDocument();
    });
  });

  it("displays evaluation settings section", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("Evaluation Settings")).toBeInTheDocument();
      expect(screen.getByText("QA Enabled")).toBeInTheDocument();
      expect(screen.getByText("Auto-Evaluate Calls")).toBeInTheDocument();
      expect(screen.getByText("Pass Threshold")).toBeInTheDocument();
      expect(screen.getByText("Evaluation Model")).toBeInTheDocument();
    });
  });

  it("shows 'Using Global' badge when inheriting settings", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("Using Global")).toBeInTheDocument();
    });
  });

  it("displays save and reset buttons", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /reset/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /save/i })).toBeInTheDocument();
    });
  });

  it("buttons are disabled when no changes made", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      const resetButton = screen.getByRole("button", { name: /reset/i });
      const saveButton = screen.getByRole("button", { name: /save/i });
      expect(resetButton).toBeDisabled();
      expect(saveButton).toBeDisabled();
    });
  });

  it("shows error state when settings fail to load", async () => {
    server.use(
      http.get(`${API_URL}/api/v1/qa/workspace/:workspaceId/settings`, () => {
        return HttpResponse.json({ detail: "Failed to load settings" }, { status: 500 });
      })
    );

    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("Failed to load QA settings")).toBeInTheDocument();
    });
  });

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

    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("Disabled")).toBeInTheDocument();
    });
  });

  it("shows API key missing status when not configured", async () => {
    server.use(
      http.get(`${API_URL}/api/v1/qa/status`, () => {
        return HttpResponse.json({
          enabled: true,
          auto_evaluate: true,
          evaluation_model: "claude-sonnet-4-20250514",
          default_threshold: 70,
          api_key_configured: false,
        });
      })
    );

    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("Missing")).toBeInTheDocument();
    });
  });

  it("enables workspace-specific settings when toggle is clicked", async () => {
    const user = userEvent.setup();
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    // Wait for data to load first
    await waitFor(() => {
      expect(screen.getByText("QA Enabled")).toBeInTheDocument();
    });

    // Find the workspace override toggle by its id (first switch that's not disabled)
    const toggles = screen.getAllByRole("switch");
    const inheritToggle = toggles.find((t) => t.id === "inherit-global");
    expect(inheritToggle).toBeDefined();

    // Click to enable workspace-specific settings
    if (!inheritToggle) throw new Error("inheritToggle not found");
    await user.click(inheritToggle);

    // Save button should now be enabled
    await waitFor(() => {
      const saveButton = screen.getByRole("button", { name: /save/i });
      expect(saveButton).not.toBeDisabled();
    });
  });

  it("displays loading skeletons while fetching data", () => {
    // Delay the response to see loading state
    server.use(
      http.get(`${API_URL}/api/v1/qa/status`, async () => {
        await new Promise((resolve) => setTimeout(resolve, 100));
        return HttpResponse.json({
          enabled: true,
          auto_evaluate: true,
          evaluation_model: "claude-sonnet-4-20250514",
          default_threshold: 70,
          api_key_configured: true,
        });
      }),
      http.get(`${API_URL}/api/v1/qa/workspace/:workspaceId/settings`, async () => {
        await new Promise((resolve) => setTimeout(resolve, 100));
        return HttpResponse.json({
          settings: {
            qa_enabled: true,
            auto_evaluate: true,
            pass_threshold: 70,
            evaluation_model: "claude-sonnet-4-20250514",
            inherit_global: true,
          },
          effective_settings: {
            qa_enabled: true,
            auto_evaluate: true,
            pass_threshold: 70,
            evaluation_model: "claude-sonnet-4-20250514",
          },
        });
      })
    );

    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    // Should show skeleton loading states (uses animate-pulse class)
    const skeletons = document.querySelectorAll('[class*="animate-pulse"]');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("displays pass threshold value", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      // The threshold value (70) should be displayed
      expect(screen.getByText("70")).toBeInTheDocument();
    });
  });

  it("displays threshold scale labels", async () => {
    renderWithProviders(<QASettingsPanel workspaceId={workspaceId} />);

    await waitFor(() => {
      expect(screen.getByText("0 (Lenient)")).toBeInTheDocument();
      expect(screen.getByText("100 (Strict)")).toBeInTheDocument();
    });
  });
});
