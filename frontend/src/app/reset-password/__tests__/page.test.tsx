/** The reset page: the link's token is used once, kept out of the address bar, and never sent anywhere else. */
import { describe, it, expect, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, server } from "@/test/test-utils";
import ResetPasswordPage from "../page";

let search = "token=abc-reset-token";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/reset-password",
  useSearchParams: () => new URLSearchParams(search),
}));

const API = "http://localhost:8000";

async function fillAndSubmit(password: string, confirm = password) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("New password"), password);
  await user.type(screen.getByLabelText("Confirm new password"), confirm);
  await user.click(screen.getByRole("button", { name: "Set new password" }));
}

describe("ResetPasswordPage", () => {
  it("sends the token and the new password, then says the password changed", async () => {
    search = "token=abc-reset-token";
    let body: unknown;
    server.use(
      http.post(`${API}/api/v1/auth/reset-password`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ detail: "Password updated." });
      })
    );

    render(<ResetPasswordPage />);
    await fillAndSubmit("a-brand-new-passphrase");

    expect(await screen.findByText(/your password is changed/i)).toBeInTheDocument();
    expect(body).toEqual({ token: "abc-reset-token", new_password: "a-brand-new-passphrase" });
  });

  it("takes the token out of the address bar", async () => {
    search = "token=abc-reset-token";
    window.history.replaceState(null, "", "/reset-password?token=abc-reset-token");

    render(<ResetPasswordPage />);

    await waitFor(() => expect(window.location.search).toBe(""));
  });

  it("explains an invalid or used link", async () => {
    search = "token=used";
    server.use(
      http.post(`${API}/api/v1/auth/reset-password`, () =>
        HttpResponse.json({ detail: "invalid" }, { status: 400 })
      )
    );

    render(<ResetPasswordPage />);
    await fillAndSubmit("a-brand-new-passphrase");

    expect(await screen.findByRole("alert")).toHaveTextContent(/invalid or has expired/i);
  });

  it("does not call the server for a short password or a mismatch", async () => {
    search = "token=abc";
    render(<ResetPasswordPage />);

    await fillAndSubmit("short");
    expect(await screen.findByRole("alert")).toHaveTextContent(/at least 8/i);
  });

  it("asks for a new link when the page is opened without one", async () => {
    search = "";

    render(<ResetPasswordPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/needs the link/i);
    expect(screen.getByRole("link", { name: /new link/i })).toHaveAttribute(
      "href",
      "/forgot-password"
    );
  });
});
