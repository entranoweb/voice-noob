/**
 * While sign-up is closed the login page must not offer it. The backend decides
 * (GET /api/v1/auth/config); the page only reflects the answer.
 */
import { describe, it, expect, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, server } from "@/test/test-utils";
import LoginPage from "../page";

vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({ login: vi.fn() }),
}));

const API = "http://localhost:8000";

function signupIs(open: boolean) {
  server.use(http.get(`${API}/api/v1/auth/config`, () => HttpResponse.json({ signup_open: open })));
}

describe("LoginPage sign-up link", () => {
  it("hides the Sign up link while sign-up is closed", async () => {
    let asked = false;
    server.use(
      http.get(`${API}/api/v1/auth/config`, () => {
        asked = true;
        return HttpResponse.json({ signup_open: false });
      })
    );

    render(<LoginPage />);

    await waitFor(() => expect(asked).toBe(true));
    expect(screen.queryByRole("link", { name: "Sign up" })).not.toBeInTheDocument();
    expect(screen.queryByText(/don.t have an account/i)).not.toBeInTheDocument();
  });

  it("shows the Sign up link when sign-up is open", async () => {
    signupIs(true);

    render(<LoginPage />);

    const link = await screen.findByRole("link", { name: "Sign up" });
    expect(link).toHaveAttribute("href", "/register");
  });

  it("keeps the link hidden when the config cannot be read", async () => {
    server.use(http.get(`${API}/api/v1/auth/config`, () => HttpResponse.error()));

    render(<LoginPage />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled());
    expect(screen.queryByRole("link", { name: "Sign up" })).not.toBeInTheDocument();
  });

  it("links to the password reset", async () => {
    signupIs(false);

    render(<LoginPage />);

    const link = await screen.findByRole("link", { name: /forgot your password/i });
    expect(link).toHaveAttribute("href", "/forgot-password");
  });
});
