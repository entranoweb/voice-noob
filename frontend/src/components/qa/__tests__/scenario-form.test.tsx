/**
 * Tests for ScenarioForm component (Task 5.1)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { server } from "@/test/test-utils";
import { ScenarioForm } from "../scenario-form";
import type { TestScenario } from "@/lib/api/qa";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const renderWithProviders = (component: React.ReactNode) => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>{component}</QueryClientProvider>
  );
};

const mockScenario: TestScenario = {
  id: "scenario-1",
  name: "Test Scenario",
  description: "A test scenario description",
  category: "booking",
  difficulty: "medium",
  is_built_in: false,
  is_active: true,
  caller_persona: { name: "John Doe", mood: "friendly", goal: "Book an appointment", context: "Regular customer" },
  initial_message: null,
  expected_behaviors: ["Greet customer", "Ask for date"],
  failure_conditions: null,
  success_criteria: { items: [{ criterion: "Booking confirmed", required: true }] },
  max_turns: 10,
  created_at: "2024-01-01T00:00:00Z",
};

describe("ScenarioForm", () => {
  beforeEach(() => { server.resetHandlers(); });
  afterEach(() => { vi.clearAllMocks(); });

  it("does not render dialog when open is false", () => {
    renderWithProviders(<ScenarioForm open={false} onOpenChange={vi.fn()} mode="create" />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders dialog when open is true", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} mode="create" />);
    await waitFor(() => { expect(screen.getByRole("dialog")).toBeInTheDocument(); });
  });

  it("renders create dialog with correct title", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} mode="create" />);
    await waitFor(() => { expect(screen.getByText("Create Test Scenario")).toBeInTheDocument(); });
  });

  it("displays Create Scenario button", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} mode="create" />);
    await waitFor(() => { expect(screen.getByRole("button", { name: /create scenario/i })).toBeInTheDocument(); });
  });

  it("displays Cancel button", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} mode="create" />);
    await waitFor(() => { expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument(); });
  });

  it("calls onOpenChange when Cancel is clicked", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    renderWithProviders(<ScenarioForm open={true} onOpenChange={onOpenChange} mode="create" />);
    await waitFor(() => { expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument(); });
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("displays form sections in accordion", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} mode="create" />);
    await waitFor(() => {
      expect(screen.getByText("Basic Information")).toBeInTheDocument();
      expect(screen.getByText("Caller Persona")).toBeInTheDocument();
      expect(screen.getByText("Conversation Flow")).toBeInTheDocument();
      expect(screen.getByText("Success Criteria")).toBeInTheDocument();
    });
  });

  it("displays scenario name input field", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} mode="create" />);
    await waitFor(() => { 
      const nameInput = screen.getByPlaceholderText(/vip client booking request/i);
      expect(nameInput).toBeInTheDocument(); 
    });
  });

  it("displays Add Turn button", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} mode="create" />);
    await waitFor(() => { expect(screen.getByRole("button", { name: /add turn/i })).toBeInTheDocument(); });
  });

  it("displays Add Criterion button", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} mode="create" />);
    await waitFor(() => { expect(screen.getByRole("button", { name: /add criterion/i })).toBeInTheDocument(); });
  });

  it("renders edit dialog with correct title", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} scenario={mockScenario} mode="edit" />);
    await waitFor(() => { expect(screen.getByText("Edit Scenario")).toBeInTheDocument(); });
  });

  it("displays Update Scenario button", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} scenario={mockScenario} mode="edit" />);
    await waitFor(() => { expect(screen.getByRole("button", { name: /update scenario/i })).toBeInTheDocument(); });
  });

  it("pre-fills form with scenario name", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} scenario={mockScenario} mode="edit" />);
    await waitFor(() => {
      const nameInput = screen.getByPlaceholderText(/vip client booking request/i) as HTMLInputElement;
      expect(nameInput.value).toBe("Test Scenario");
    });
  });

  it("renders view dialog with correct title", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} scenario={mockScenario} mode="view" />);
    await waitFor(() => { expect(screen.getByText("View Scenario")).toBeInTheDocument(); });
  });

  it("displays Close button in view mode", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} scenario={mockScenario} mode="view" />);
    await waitFor(() => {
      const closeButtons = screen.getAllByRole("button", { name: /close/i });
      expect(closeButtons.length).toBeGreaterThan(0);
    });
  });

  it("does not display Create/Update button in view mode", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} scenario={mockScenario} mode="view" />);
    await waitFor(() => { expect(screen.getByRole("dialog")).toBeInTheDocument(); });
    expect(screen.queryByRole("button", { name: /create scenario/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /update scenario/i })).not.toBeInTheDocument();
  });

  it("disables scenario name input in view mode", async () => {
    renderWithProviders(<ScenarioForm open={true} onOpenChange={vi.fn()} scenario={mockScenario} mode="view" />);
    await waitFor(() => {
      const nameInput = screen.getByPlaceholderText(/vip client booking request/i) as HTMLInputElement;
      expect(nameInput).toBeDisabled();
    });
  });
});