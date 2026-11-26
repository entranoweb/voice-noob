/** The forgot-password page gives one answer whether or not the address has an account. */
import { describe, it, expect } from "vitest";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { render, screen, server } from "@/test/test-utils";
import ForgotPasswordPage from "../page";

const API = "http://localhost:8000";

describe("ForgotPasswordPage", () => {
  it("shows the same message after any accepted request", async () => {
    let sent: unknown;
    server.use(
      http.post(`${API}/api/v1/auth/forgot-password`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json({ detail: "x" }, { status: 202 });
      })
    );
    const user = userEvent.setup();

    render(<ForgotPasswordPage />);
    await user.type(screen.getByLabelText("Email"), "someone@example.com");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "If that address has an account, a reset link is on its way."
    );
    expect(sent).toEqual({ email: "someone@example.com" });
  });

  it("says so when the rate limit is hit", async () => {
    server.use(
      http.post(`${API}/api/v1/auth/forgot-password`, () =>
        HttpResponse.json({ detail: "slow down" }, { status: 429 })
      )
    );
    const user = userEvent.setup();

    render(<ForgotPasswordPage />);
    await user.type(screen.getByLabelText("Email"), "someone@example.com");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/too many requests/i);
  });
});
