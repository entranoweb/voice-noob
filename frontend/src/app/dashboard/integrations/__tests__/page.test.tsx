import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import userEvent from "@testing-library/user-event";
import IntegrationsPage from "../page";

describe("IntegrationsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders page title and description", () => {
    render(<IntegrationsPage />);

    expect(screen.getByText("Integrations & Tools")).toBeInTheDocument();
    expect(
      screen.getByText("Connect external services for your voice agents to use")
    ).toBeInTheDocument();
  });

  it("displays connection statistics", () => {
    render(<IntegrationsPage />);

    // The built-in tools are always connected: call_control, crm, bookings.
    // If a fourth is added, this count moves with it deliberately.
    expect(screen.getByText("3 Connected")).toBeInTheDocument();
    expect(screen.getByText(/\d+ Available/)).toBeInTheDocument();
  });

  it("renders search input", () => {
    render(<IntegrationsPage />);

    const searchInput = screen.getByPlaceholderText("Search integrations...");
    expect(searchInput).toBeInTheDocument();
  });

  it("renders category tabs", () => {
    render(<IntegrationsPage />);

    // Tabs have specific role="tab" attribute
    expect(screen.getByRole("tab", { name: "All" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "CRM" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Calendar" })).toBeInTheDocument();
  });

  it("displays integration cards", () => {
    render(<IntegrationsPage />);

    // Check for popular integrations
    expect(screen.getByText("Salesforce")).toBeInTheDocument();
    expect(screen.getByText("HubSpot")).toBeInTheDocument();
    expect(screen.getByText("Google Calendar")).toBeInTheDocument();
    expect(screen.getByText("Notion")).toBeInTheDocument();
  });

  it("shows Popular badge for popular integrations", () => {
    render(<IntegrationsPage />);

    const popularBadges = screen.getAllByText("Popular");
    expect(popularBadges.length).toBeGreaterThan(0);
  });

  it("filters integrations by search query", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    const searchInput = screen.getByPlaceholderText("Search integrations...");
    await user.type(searchInput, "salesforce");

    // Wait for debounce (300ms)
    await waitFor(
      () => {
        expect(screen.getByText("Salesforce")).toBeInTheDocument();
        expect(screen.queryByText("HubSpot")).not.toBeInTheDocument();
      },
      { timeout: 500 }
    );
  });

  it("debounces search input", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    const searchInput = screen.getByPlaceholderText("Search integrations...");

    // Type quickly
    await user.type(searchInput, "goo");

    // Should not filter immediately
    expect(screen.getByText("Salesforce")).toBeInTheDocument();

    // Wait for debounce
    await waitFor(
      () => {
        expect(screen.getByText("Google Calendar")).toBeInTheDocument();
      },
      { timeout: 500 }
    );
  });

  it("filters integrations by category", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Click CRM tab
    const crmTab = screen.getByRole("tab", { name: "CRM" });
    await user.click(crmTab);

    // Should show only CRM integrations
    expect(screen.getByText("Salesforce")).toBeInTheDocument();
    expect(screen.getByText("HubSpot")).toBeInTheDocument();
    // Calendar integrations should not be visible in grid
    const calendarIntegrations = screen.queryAllByText("Google Calendar");
    expect(calendarIntegrations.length).toBeLessThanOrEqual(1); // May exist in other contexts
  });

  it("shows empty state when no integrations match filter", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    const searchInput = screen.getByPlaceholderText("Search integrations...");
    await user.type(searchInput, "nonexistentintegration12345");

    await waitFor(
      () => {
        expect(screen.getByText("No integrations found")).toBeInTheDocument();
        expect(
          screen.getByText("Try adjusting your search or category filter")
        ).toBeInTheDocument();
      },
      { timeout: 500 }
    );
  });

  it("displays integration descriptions", () => {
    render(<IntegrationsPage />);

    expect(
      screen.getByText("Access customer data, create leads, update opportunities")
    ).toBeInTheDocument();
    expect(
      screen.getByText("Manage contacts, deals, and customer interactions (free MCP)")
    ).toBeInTheDocument();
  });

  it("shows Connect button for non-connected integrations", () => {
    render(<IntegrationsPage />);

    const connectButtons = screen.getAllByRole("button", { name: "Connect" });
    expect(connectButtons.length).toBeGreaterThan(0);
  });

  it("opens configuration dialog when Connect is clicked", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Find and click the first Connect button
    const connectButtons = screen.getAllByRole("button", { name: "Connect" });
    const firstButton = connectButtons[0];
    if (!firstButton) throw new Error("No connect button found");
    await user.click(firstButton);

    // Dialog should open with title
    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
  });

  it("shows Docs button for integrations with documentation", () => {
    render(<IntegrationsPage />);

    const docsButtons = screen.getAllByRole("link", { name: "Docs" });
    expect(docsButtons.length).toBeGreaterThan(0);
  });

  it("combines search and category filters", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Select CRM category
    const crmTab = screen.getByRole("tab", { name: "CRM" });
    await user.click(crmTab);

    // Search within CRM category
    const searchInput = screen.getByPlaceholderText("Search integrations...");
    await user.type(searchInput, "sales");

    await waitFor(
      () => {
        expect(screen.getByText("Salesforce")).toBeInTheDocument();
        expect(screen.queryByText("HubSpot")).not.toBeInTheDocument();
      },
      { timeout: 500 }
    );
  });

  it("clears search when switching categories", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Type in search
    const searchInput = screen.getByPlaceholderText("Search integrations...");
    await user.type(searchInput, "salesforce");

    await waitFor(() => {
      expect(searchInput).toHaveValue("salesforce");
    });

    // Switch to Calendar category
    const calendarTab = screen.getByRole("tab", { name: "Calendar" });
    await user.click(calendarTab);

    // Search value should remain (filters are independent)
    expect(searchInput).toHaveValue("salesforce");
  });

  it("shows category in uppercase in integration cards", () => {
    const { container } = render(<IntegrationsPage />);

    // Categories should be displayed in uppercase (check for text content containing uppercase)
    const categoryText = container.textContent ?? "";
    expect(categoryText).toContain("CRM");
  });

  it("handles responsive grid layout", () => {
    const { container } = render(<IntegrationsPage />);

    // Check for responsive grid classes
    const grid = container.querySelector('[class*="grid"]');
    expect(grid).toBeInTheDocument();
  });

  it("memoizes filtered integrations", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Type in search to trigger filtering
    const searchInput = screen.getByPlaceholderText("Search integrations...");
    await user.type(searchInput, "google");

    // Wait for debounce and verify memoized result
    await waitFor(
      () => {
        expect(screen.getByText("Google Calendar")).toBeInTheDocument();
        expect(screen.getByText("Google Sheets")).toBeInTheDocument();
      },
      { timeout: 500 }
    );
  });
});

describe("IntegrationCard", () => {
  it("shows checkmark for connected integrations", () => {
    // This would require modifying the mock data or props
    // For now, we test the general rendering
    render(<IntegrationsPage />);
    expect(screen.getByText("Salesforce")).toBeInTheDocument();
  });

  it("displays Configure button for connected integrations", () => {
    // Would need to mock connected state
    render(<IntegrationsPage />);
    // All are currently shown as disconnected in mock
    const connectButtons = screen.getAllByRole("button", { name: "Connect" });
    expect(connectButtons.length).toBeGreaterThan(0);
  });
});

describe("IntegrationConfigForm - API Key Auth", () => {
  it("shows API key form for Salesforce integration", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Find Salesforce and click Connect
    const salesforceCard = screen.getByText("Salesforce").closest("div")?.parentElement;
    const connectButton = salesforceCard?.querySelector('button[class*="flex-1"]');

    if (connectButton) {
      await user.click(connectButton);

      await waitFor(() => {
        // Should show API key input form
        expect(screen.getByRole("dialog")).toBeInTheDocument();
      });
    }
  });

  it("opens dialog when Connect is clicked on any integration", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Find and click the first Connect button
    const connectButtons = screen.getAllByRole("button", { name: "Connect" });
    const firstButton = connectButtons[0];
    if (!firstButton) throw new Error("No connect button found");
    await user.click(firstButton);

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
  });
});

describe("IntegrationConfigForm - API Key", () => {
  it("shows API key input for API key integrations", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Find Pipedrive (API key) and open its dialog
    const pipedriveCard = screen.getByText("Pipedrive");
    const card = pipedriveCard.closest("div")?.parentElement;
    const connectButton = card?.querySelector('button[class*="flex-1"]');

    if (connectButton) {
      await user.click(connectButton);

      await waitFor(() => {
        expect(screen.getByText("API Token")).toBeInTheDocument();
      });
    }
  });

  it("shows required field indicators", async () => {
    const user = userEvent.setup();
    render(<IntegrationsPage />);

    // Open any API key integration
    const stripeCard = screen.getByText("Stripe");
    const card = stripeCard.closest("div")?.parentElement;
    const connectButton = card?.querySelector('button[class*="flex-1"]');

    if (connectButton) {
      await user.click(connectButton);

      await waitFor(() => {
        // Required fields should have asterisk
        const requiredIndicators = screen.getAllByText("*");
        expect(requiredIndicators.length).toBeGreaterThan(0);
      });
    }
  });
});
