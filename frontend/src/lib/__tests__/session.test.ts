/** Silent refresh: one call at a time, the new pair stored, a refused token ends the session. */
import { describe, it, expect, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "@/test/test-utils";
import {
  accessTokenExpiry,
  clearSession,
  getAccessToken,
  getRefreshToken,
  refreshSession,
  storeSession,
} from "../session";

const API = "http://localhost:8000";

describe("refreshSession", () => {
  beforeEach(() => {
    clearSession();
    storeSession({ access_token: "old-access", refresh_token: "old-refresh" });
  });

  it("stores the new pair", async () => {
    server.use(
      http.post(`${API}/api/v1/auth/refresh`, () =>
        HttpResponse.json({ access_token: "new-access", refresh_token: "new-refresh" })
      )
    );

    expect(await refreshSession()).toBe("new-access");
    expect(getAccessToken()).toBe("new-access");
    expect(getRefreshToken()).toBe("new-refresh");
  });

  it("shares one request between callers, since a refresh token works once", async () => {
    let calls = 0;
    server.use(
      http.post(`${API}/api/v1/auth/refresh`, async () => {
        calls += 1;
        await new Promise((resolve) => setTimeout(resolve, 20));
        return HttpResponse.json({ access_token: "new-access", refresh_token: "new-refresh" });
      })
    );

    const results = await Promise.all([refreshSession(), refreshSession(), refreshSession()]);

    expect(results).toEqual(["new-access", "new-access", "new-access"]);
    expect(calls).toBe(1);
  });

  it("clears the session when the server refuses the token", async () => {
    server.use(
      http.post(`${API}/api/v1/auth/refresh`, () =>
        HttpResponse.json({ detail: "no" }, { status: 401 })
      )
    );

    expect(await refreshSession()).toBeNull();
    expect(getAccessToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });

  it("keeps the session on a network error", async () => {
    server.use(http.post(`${API}/api/v1/auth/refresh`, () => HttpResponse.error()));

    expect(await refreshSession()).toBeNull();
    expect(getRefreshToken()).toBe("old-refresh");
  });

  it("does nothing without a refresh token", async () => {
    clearSession();

    expect(await refreshSession()).toBeNull();
  });
});

describe("accessTokenExpiry", () => {
  it("reads exp from the payload", () => {
    const payload = btoa(JSON.stringify({ exp: 1_800_000_000 }));
    expect(accessTokenExpiry(`h.${payload}.s`)).toBe(1_800_000_000_000);
  });

  it("is null for anything that is not a token", () => {
    expect(accessTokenExpiry("nonsense")).toBeNull();
  });
});
