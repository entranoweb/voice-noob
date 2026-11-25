/** API token management shows the secret once; the workspace id is shown and copyable. */
import { describe, it, expect } from "vitest";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { render, screen, server } from "@/test/test-utils";
import { ApiTokensPanel } from "../api-tokens-panel";
import { WorkspaceId } from "../workspace-id";

const API = "http://localhost:8000";

const summary = {
  id: "t1",
  name: "CI upload",
  token_prefix: "sqt_abcd1234",
  scope: "caller:upload",
  created_at: "2026-11-25T10:00:00Z",
  last_used_at: null,
  expires_at: null,
  revoked_at: null,
};

describe("ApiTokensPanel", () => {
  it("lists tokens by prefix, never the secret", async () => {
    server.use(http.get(`${API}/api/v1/auth/api-tokens`, () => HttpResponse.json([summary])));

    render(<ApiTokensPanel />);

    expect(await screen.findByText("CI upload")).toBeInTheDocument();
    expect(screen.getByText("sqt_abcd1234...")).toBeInTheDocument();
  });

  it("shows a new secret once, then drops it", async () => {
    let created: unknown;
    server.use(
      http.get(`${API}/api/v1/auth/api-tokens`, () => HttpResponse.json([])),
      http.post(`${API}/api/v1/auth/api-tokens`, async ({ request }) => {
        created = await request.json();
        return HttpResponse.json({ ...summary, token: "sqt_the-full-secret" }, { status: 201 });
      })
    );
    const user = userEvent.setup();

    render(<ApiTokensPanel />);
    await user.type(screen.getByLabelText("Token name"), "CI upload");
    await user.type(screen.getByLabelText(/expires after/i), "30");
    await user.click(screen.getByRole("button", { name: "Create token" }));

    expect(await screen.findByTestId("new-api-token")).toHaveTextContent("sqt_the-full-secret");
    expect(created).toEqual({ name: "CI upload", expires_in_days: 30 });

    await user.click(screen.getByRole("button", { name: /i have copied it/i }));
    expect(screen.queryByTestId("new-api-token")).not.toBeInTheDocument();
  });

  it("revokes a token", async () => {
    let revoked = "";
    server.use(
      http.get(`${API}/api/v1/auth/api-tokens`, () => HttpResponse.json([summary])),
      http.delete(`${API}/api/v1/auth/api-tokens/:id`, ({ params }) => {
        revoked = String(params.id);
        return new HttpResponse(null, { status: 204 });
      })
    );
    const user = userEvent.setup();

    render(<ApiTokensPanel />);
    await user.click(await screen.findByRole("button", { name: "Revoke CI upload" }));

    await screen.findByText("CI upload");
    expect(revoked).toBe("t1");
  });
});

describe("WorkspaceId", () => {
  it("shows the id and copies it", async () => {
    const user = userEvent.setup();
    render(<WorkspaceId workspaceId="ws-123" workspaceName="Main" />);

    expect(screen.getByTestId("workspace-id")).toHaveTextContent("ws-123");
    await user.click(screen.getByRole("button", { name: "Copy workspace ID" }));

    expect(await navigator.clipboard.readText()).toBe("ws-123");
  });
});
