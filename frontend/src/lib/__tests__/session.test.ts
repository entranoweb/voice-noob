/** Silent refresh: one call at a time, the new pair stored, a refused token ends the session. */
import { describe, it, expect, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "@/test/test-utils";
import { getCallStats } from "@/lib/api/calls";
import {
  accessTokenExpiry,
  authedFetch,
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

describe("authedFetch", () => {
  beforeEach(() => {
    clearSession();
    storeSession({ access_token: "old-access", refresh_token: "old-refresh" });
  });

  function serve(seen: string[], refreshCalls: { n: number }) {
    server.use(
      http.get(`${API}/api/v1/probe`, ({ request }) => {
        const auth = request.headers.get("authorization") ?? "";
        seen.push(auth);
        return auth === "Bearer new-access"
          ? HttpResponse.json({ ok: true })
          : HttpResponse.json({ detail: "expired" }, { status: 401 });
      }),
      http.post(`${API}/api/v1/auth/refresh`, () => {
        refreshCalls.n += 1;
        return HttpResponse.json({ access_token: "new-access", refresh_token: "new-refresh" });
      })
    );
  }

  it("on a 401 refreshes once and retries with the new token", async () => {
    const seen: string[] = [];
    const refreshCalls = { n: 0 };
    serve(seen, refreshCalls);

    const response = await authedFetch(`${API}/api/v1/probe`);

    expect(response.status).toBe(200);
    expect(refreshCalls.n).toBe(1);
    expect(seen).toEqual(["Bearer old-access", "Bearer new-access"]);
  });

  it("gives up after one retry and returns the 401 when the refresh is refused", async () => {
    server.use(
      http.get(`${API}/api/v1/probe`, () => HttpResponse.json({ detail: "x" }, { status: 401 })),
      http.post(`${API}/api/v1/auth/refresh`, () => HttpResponse.json({}, { status: 401 }))
    );

    const response = await authedFetch(`${API}/api/v1/probe`);

    expect(response.status).toBe(401);
    expect(getAccessToken()).toBeNull();
  });

  it("is used by the call-history client", async () => {
    const seen: string[] = [];
    const refreshCalls = { n: 0 };
    server.use(
      http.get(`${API}/api/v1/calls/stats`, ({ request }) => {
        const auth = request.headers.get("authorization") ?? "";
        seen.push(auth);
        return auth === "Bearer new-access"
          ? HttpResponse.json({ total_calls: 3 })
          : HttpResponse.json({ detail: "expired" }, { status: 401 });
      }),
      http.post(`${API}/api/v1/auth/refresh`, () => {
        refreshCalls.n += 1;
        return HttpResponse.json({ access_token: "new-access", refresh_token: "new-refresh" });
      })
    );

    await getCallStats();

    expect(refreshCalls.n).toBe(1);
    expect(seen).toEqual(["Bearer old-access", "Bearer new-access"]);
  });
});
