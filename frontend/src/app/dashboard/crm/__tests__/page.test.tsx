import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@/test/test-utils";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import CRMPage from "../page";
import { api } from "@/lib/api";

// Mock the API
vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(),
  },
}));

const mockContacts = [
  {
    id: 1,
    user_id: 1,
    first_name: "John",
    last_name: "Doe",
    email: "john.doe@example.com",
    phone_number: "+1234567890",
    company_name: "Acme Corp",
    status: "qualified",
    tags: "vip,enterprise",
    notes: "Important client",
  },
  {
    id: 2,
    user_id: 1,
    first_name: "Jane",
    last_name: "Smith",
    email: "jane.smith@example.com",
    phone_number: "+0987654321",
    company_name: "TechStart",
    status: "new",
    tags: "startup",
    notes: null,
  },
  {
    id: 3,
    user_id: 1,
    first_name: "Bob",
    last_name: null,
    email: null,
    phone_number: "+1122334455",
    company_name: null,
    status: "contacted",
    tags: null,
    notes: "Follow up needed",
  },
];

/** Read the number rendered under a stat card's label. */
function statValue(container: HTMLElement, label: string): string | undefined {
  const heading = Array.from(container.querySelectorAll("p")).find(
    (node) => node.textContent === label
  );
  return heading?.nextElementSibling?.textContent ?? undefined;
}

describe("CRMPage", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: {
        queries: {
          retry: false,
        },
      },
    });
    vi.clearAllMocks();
  });

  const renderWithClient = (ui: React.ReactElement) => {
    return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  };

  it("renders page title and description", () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] });
    renderWithClient(<CRMPage />);

    expect(screen.getByText("CRM")).toBeInTheDocument();
    expect(screen.getByText("Manage your contacts and interactions")).toBeInTheDocument();
  });

  it("renders Add Contact button", () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] });
    renderWithClient(<CRMPage />);

    expect(screen.getByRole("button", { name: /Add Contact/i })).toBeInTheDocument();
  });

  it("displays stats cards", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("Total Contacts")).toBeInTheDocument();
      expect(screen.getByText("Appointments")).toBeInTheDocument();
      expect(screen.getByText("Call Interactions")).toBeInTheDocument();
    });
  });

  it("shows loading state", async () => {
    vi.mocked(api.get).mockImplementation(
      () => new Promise(() => {}) // Never resolves
    );
    renderWithClient(<CRMPage />);

    // Wait briefly for component to mount and show loading state
    await waitFor(
      () => {
        expect(screen.getAllByText("Loading contacts...").length).toBeGreaterThan(0);
      },
      { timeout: 500 }
    );
  });

  it("displays contact count in stats", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("3")).toBeInTheDocument(); // Total contacts count
    });
  });

  it("renders contacts list when data is loaded", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("John Doe")).toBeInTheDocument();
      expect(screen.getByText("Jane Smith")).toBeInTheDocument();
      expect(screen.getByText("Bob")).toBeInTheDocument();
    });
  });

  it("displays contact details correctly", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      // Check email
      expect(screen.getByText("john.doe@example.com")).toBeInTheDocument();

      // Check phone number
      expect(screen.getByText("+1234567890")).toBeInTheDocument();

      // Check company
      expect(screen.getByText("Acme Corp")).toBeInTheDocument();

      // Check tags
      expect(screen.getByText("vip,enterprise")).toBeInTheDocument();
    });
  });

  it("displays status badges with correct styling", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("qualified")).toBeInTheDocument();
      expect(screen.getByText("new")).toBeInTheDocument();
      expect(screen.getByText("contacted")).toBeInTheDocument();
    });
  });

  it("shows empty state when no contacts exist", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("No contacts yet")).toBeInTheDocument();
      expect(
        screen.getByText(/Add contacts manually or they'll be created automatically/)
      ).toBeInTheDocument();
    });
  });

  it("shows Add Your First Contact button in empty state", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Add Your First Contact/i })).toBeInTheDocument();
    });
  });

  it("counts the loaded contacts in the stats card", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    const { container } = renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(statValue(container, "Total Contacts")).toBe("3");
    });
  });

  it("counts a single contact correctly", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [mockContacts[0]] });
    const { container } = renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(statValue(container, "Total Contacts")).toBe("1");
    });
  });

  it("handles error state gracefully", async () => {
    const errorMessage = "Failed to fetch contacts";
    vi.mocked(api.get).mockRejectedValue(new Error(errorMessage));
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("Failed to load contacts")).toBeInTheDocument();
      expect(screen.getByText(errorMessage)).toBeInTheDocument();
    });
  });

  it("displays error icon in error state", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("Network error"));
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("Failed to load contacts")).toBeInTheDocument();
    });
  });

  it("opens the details dialog when a contact card is clicked", async () => {
    // The card itself is the control now; there is no separate View Details
    // button. What matters is that a contact is still one click from its detail.
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("John Doe")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("John Doe"));

    expect(await screen.findByText("Contact Details")).toBeInTheDocument();
  });

  it("handles contacts with null fields gracefully", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [mockContacts[2]] }); // Bob with null fields
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("Bob")).toBeInTheDocument();
      expect(screen.getByText("+1122334455")).toBeInTheDocument();
      // Should not crash with null email, company, tags
    });
  });

  it("applies correct status colors", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    const { container } = renderWithClient(<CRMPage />);

    await waitFor(() => {
      // Check for status badge classes
      const statusBadges = container.querySelectorAll('[class*="rounded-full"]');
      expect(statusBadges.length).toBeGreaterThan(0);
    });
  });

  it("displays loading spinner during data fetch", async () => {
    vi.mocked(api.get).mockImplementation(
      () => new Promise(() => {}) // Never resolves
    );
    renderWithClient(<CRMPage />);

    // Should show loader icon
    await waitFor(
      () => {
        expect(screen.getAllByText("Loading contacts...").length).toBeGreaterThan(0);
      },
      { timeout: 500 }
    );
  });

  it("fetches contacts from correct API endpoint", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(api.get).toHaveBeenCalledWith("/api/v1/crm/contacts");
    });
  });

  it("uses React Query for data fetching", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    renderWithClient(<CRMPage />);

    // Should use 'contacts' query key
    await waitFor(() => {
      expect(screen.getByText("John Doe")).toBeInTheDocument();
    });

    // The key is scoped by workspace, so match on its prefix rather than
    // pinning the test to whichever workspace happens to be selected.
    const cached = queryClient.getQueriesData({ queryKey: ["contacts"] });
    expect(cached.map(([, data]) => data)).toContainEqual(mockContacts);
  });

  it("displays appointments count (currently 0)", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    const { container } = renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(statValue(container, "Appointments")).toBe("0");
    });
  });

  it("displays call interactions count (currently 0)", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    const { container } = renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(statValue(container, "Call Interactions")).toBe("0");
    });
  });

  it("renders contact cards with hover effect", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    const { container } = renderWithClient(<CRMPage />);

    await waitFor(() => {
      const contactCards = container.querySelectorAll('[class*="hover:border-primary"]');
      expect(contactCards.length).toBeGreaterThan(0);
    });
  });

  it("displays icons for contact information", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: mockContacts });
    const { container } = renderWithClient(<CRMPage />);

    await waitFor(() => {
      // Should have phone, mail, building, tag icons
      const icons = container.querySelectorAll("svg");
      expect(icons.length).toBeGreaterThan(0);
    });
  });

  it("handles generic error objects", async () => {
    vi.mocked(api.get).mockRejectedValue({ message: "Unknown error" });
    renderWithClient(<CRMPage />);

    await waitFor(() => {
      expect(screen.getByText("Failed to load contacts")).toBeInTheDocument();
      expect(screen.getByText("An error occurred")).toBeInTheDocument();
    });
  });
});
