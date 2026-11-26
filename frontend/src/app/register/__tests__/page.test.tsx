/** A closed sign-up sends the visitor to the login page instead of showing a form the server refuses. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, server } from "@/test/test-utils";
import RegisterPage from "../page";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace }),
  usePathname: () => "/register",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({ register: vi.fn() }),
}));

const API = "http://localhost:8000";

describe("RegisterPage", () => {
  beforeEach(() => replace.mockClear());

  it("redirects to /login while sign-up is closed, without showing the form", async () => {
    server.use(
      http.get(`${API}/api/v1/auth/config`, () => HttpResponse.json({ signup_open: false }))
    );

    render(<RegisterPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByRole("button", { name: /create account/i })).not.toBeInTheDocument();
  });

  it("shows the form when sign-up is open", async () => {
    server.use(
      http.get(`${API}/api/v1/auth/config`, () => HttpResponse.json({ signup_open: true }))
    );

    render(<RegisterPage />);

    expect(await screen.findByText("Create an account")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });
});
