/** A 409 from /refresh means "lost a race", never "signed out". */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, server } from "@/test/test-utils";
import { AuthProvider, useAuth } from "@/hooks/use-auth";

// The global setup replaces the hook; this test needs the real one.
vi.mock("@/hooks/use-auth", async () => await vi.importActual("@/hooks/use-auth"));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/dashboard",
}));

const API = "http://localhost:8000";

function Probe() {
  const { user, isLoading } = useAuth();
  return <div>{isLoading ? "loading" : user ? `user:${user.email}` : "nobody"}</div>;
}

describe("signing in again on page load", () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem("access_token", "expired");
    localStorage.setItem("refresh_token", "r0");
  });

  it("keeps the session when /refresh answers 409 and nothing newer shows up", async () => {
    server.use(
      http.get(`${API}/api/v1/auth/me`, () => HttpResponse.json({ detail: "x" }, { status: 401 })),
      http.post(`${API}/api/v1/auth/refresh`, () =>
        HttpResponse.json({ detail: "just rotated" }, { status: 409 })
      )
    );

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await waitFor(() => expect(screen.queryByText("loading")).not.toBeInTheDocument(), {
      timeout: 5000,
    });
    expect(localStorage.getItem("refresh_token")).toBe("r0");
    expect(localStorage.getItem("access_token")).toBe("expired");
  }, 10000);

  it("still ends the session when /refresh refuses the token (401)", async () => {
    server.use(
      http.get(`${API}/api/v1/auth/me`, () => HttpResponse.json({ detail: "x" }, { status: 401 })),
      http.post(`${API}/api/v1/auth/refresh`, () =>
        HttpResponse.json({ detail: "no" }, { status: 401 })
      )
    );

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await waitFor(() => expect(screen.getByText("nobody")).toBeInTheDocument());
    expect(localStorage.getItem("refresh_token")).toBeNull();
  });
});
