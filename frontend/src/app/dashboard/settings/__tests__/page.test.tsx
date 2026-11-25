/**
 * The settings page used to open on the account level, where a saved key shows
 * as set and is never read by a call: calls read keys from the agent's
 * workspace only, with no fallback.
 */
import { describe, it, expect, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, server } from "@/test/test-utils";
import SettingsPage from "../page";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const API = "http://localhost:8000";

const emptySettings = {
  openai_api_key_set: false,
  deepgram_api_key_set: false,
  elevenlabs_api_key_set: false,
  telnyx_api_key_set: false,
  twilio_account_sid_set: false,
  azure_openai_endpoint_set: false,
  azure_openai_api_key_set: false,
  azure_openai_deployment_name_set: false,
  openai_provider: "openai",
  workspace_id: null,
};

/** Records the workspace_id of every settings read. `null` is the account level. */
function recordSettingsReads() {
  const reads: Array<string | null> = [];
  server.use(
    http.get(`${API}/api/v1/settings`, ({ request }) => {
      reads.push(new URL(request.url).searchParams.get("workspace_id"));
      return HttpResponse.json(emptySettings);
    }),
    // The compliance tab reads this on mount.
    http.get(`${API}/api/v1/compliance/privacy-settings`, () => HttpResponse.json({}))
  );
  return reads;
}

describe("SettingsPage", () => {
  it("opens on the default workspace, not on the account level", async () => {
    server.use(
      http.get(`${API}/api/v1/workspaces`, () =>
        HttpResponse.json([
          { id: "ws-other", name: "Other", description: null, is_default: false },
          { id: "ws-default", name: "Main", description: null, is_default: true },
        ])
      )
    );
    const reads = recordSettingsReads();

    render(<SettingsPage />);

    await waitFor(() => expect(reads.length).toBeGreaterThan(0));
    expect(reads).toEqual(["ws-default"]);
    expect(screen.queryByText(/keys saved here are not used for calls/i)).not.toBeInTheDocument();
  });

  it("warns that account-level keys are not used when there is no workspace", async () => {
    server.use(http.get(`${API}/api/v1/workspaces`, () => HttpResponse.json([])));
    recordSettingsReads();

    render(<SettingsPage />);

    const warning = await screen.findByRole("alert");
    expect(warning).toHaveTextContent("Keys saved here are not used for calls.");
    expect(warning).toHaveTextContent("Create a workspace first");
  });
});
