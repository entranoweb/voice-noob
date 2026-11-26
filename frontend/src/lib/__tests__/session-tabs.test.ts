/** Two tabs share localStorage but not module state. A refresh token works once. */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "@/test/test-utils";

const API = "http://localhost:8000";
type SessionModule = typeof import("../session");

/** A tab: its own copy of the module (its own in-flight promise), the same storage. */
async function openTab(): Promise<SessionModule> {
  vi.resetModules();
  return import("../session");
}

/** navigator.locks shared by every tab: one holder at a time, in order. */
function installSharedLocks() {
  let tail: Promise<unknown> = Promise.resolve();
  Object.defineProperty(navigator, "locks", {
    configurable: true,
    value: {
      request: (_name: string, callback: () => Promise<unknown>) => {
        const run = tail.then(callback);
        tail = run.catch(() => undefined);
        return run;
      },
    },
  });
}

function serverThatRotatesOnce() {
  const state = { calls: 0, current: "r0", previous: "" };
  server.use(
    http.post(`${API}/api/v1/auth/refresh`, async ({ request }) => {
      state.calls += 1;
      const body = (await request.json()) as { refresh_token: string };
      await new Promise((resolve) => setTimeout(resolve, 10));
      if (body.refresh_token === state.previous) {
        // Rotated a moment ago: no tokens, nothing revoked.
        return HttpResponse.json({ detail: "just rotated" }, { status: 409 });
      }
      if (body.refresh_token !== state.current) {
        return HttpResponse.json({ detail: "reuse" }, { status: 401 });
      }
      state.previous = state.current;
      state.current = `r${state.calls}`;
      return HttpResponse.json({
        access_token: `a${state.calls}`,
        refresh_token: state.current,
      });
    })
  );
  return state;
}

describe("refreshing from two tabs at once", () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem("access_token", "a0");
    localStorage.setItem("refresh_token", "r0");
  });
  afterEach(() => {
    Reflect.deleteProperty(navigator, "locks");
  });

  it("sends one /refresh, and the second tab uses the pair the first stored", async () => {
    installSharedLocks();
    const state = serverThatRotatesOnce();
    const tabA = await openTab();
    const tabB = await openTab();

    const [a, b] = await Promise.all([tabA.refreshSession(), tabB.refreshSession()]);

    expect(state.calls).toBe(1);
    expect([a, b]).toEqual(["a1", "a1"]);
    expect(localStorage.getItem("refresh_token")).toBe("r1");
    expect(localStorage.getItem("access_token")).toBe("a1");
  });

  it("still works in a browser with no Web Locks, one tab at a time", async () => {
    const state = serverThatRotatesOnce();
    const tab = await openTab();

    expect(await tab.refreshSession()).toBe("a1");
    expect(state.calls).toBe(1);
  });

  /**
   * What the browser really does: the tab that gets the lock next can read the
   * value from before the previous holder's write for a few milliseconds.
   */
  function readStaleFor(ms: number, staleValue: string) {
    const real = localStorage.getItem.bind(localStorage);
    const until = Date.now() + ms;
    return vi.spyOn(localStorage, "getItem").mockImplementation((key: string) => {
      if (key === "refresh_token" && Date.now() < until) return staleValue;
      return real(key);
    });
  }

  it("a stale read after the lock hand-off ends in the stored pair, not in a sign-out", async () => {
    installSharedLocks();
    const state = serverThatRotatesOnce();
    const tabA = await openTab();
    const tabB = await openTab();
    await tabA.refreshSession();
    expect(localStorage.getItem("refresh_token")).toBe("r1");
    const stale = readStaleFor(60, "r0");

    const renewed = await tabB.refreshSession();
    stale.mockRestore();

    expect(renewed).toBe("a1");
    expect(state.calls).toBe(2);
    expect(localStorage.getItem("refresh_token")).toBe("r1");
    expect(localStorage.getItem("access_token")).toBe("a1");
  });

  it("when no newer pair ever appears it gives up quietly: one request, session kept", async () => {
    installSharedLocks();
    const state = serverThatRotatesOnce();
    state.previous = "r0"; // the token this tab holds was rotated by someone else
    state.current = "elsewhere";
    const tab = await openTab();

    const renewed = await tab.refreshSession();

    expect(renewed).toBeNull();
    expect(state.calls).toBe(1);
    expect(localStorage.getItem("refresh_token")).toBe("r0");
    expect(localStorage.getItem("access_token")).toBe("a0");
  }, 10_000);
});
