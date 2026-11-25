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
  const state = { calls: 0, current: "r0" };
  server.use(
    http.post(`${API}/api/v1/auth/refresh`, async ({ request }) => {
      state.calls += 1;
      const body = (await request.json()) as { refresh_token: string };
      await new Promise((resolve) => setTimeout(resolve, 10));
      if (body.refresh_token !== state.current) {
        return HttpResponse.json({ detail: "reuse" }, { status: 401 });
      }
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
});
