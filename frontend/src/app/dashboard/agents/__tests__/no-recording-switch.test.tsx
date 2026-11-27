/**
 * No recording is made of a call, so no agent form may offer a switch that says
 * it records. The transcript switch stays: transcripts are saved.
 */
import { describe, it, expect, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { render, screen } from "@/test/test-utils";
import NewAgentPage from "../new/page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

describe("agent form", () => {
  it("offers transcripts but no call-recording switch", async () => {
    render(<NewAgentPage />);

    await userEvent.click(screen.getByRole("tab", { name: "Advanced" }));

    expect(screen.getByText("Enable Transcripts")).toBeInTheDocument();
    expect(screen.queryByText(/call recording/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/record all calls/i)).not.toBeInTheDocument();
  });
});
