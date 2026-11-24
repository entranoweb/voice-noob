import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  FRAME_ANCESTORS_SELF,
  clearFramePolicyCache,
  frameAncestorsFor,
  safeFrameAncestors,
} from "./embed-frame-policy";

function answering(body: unknown, status = 200): typeof fetch {
  return vi.fn(async () => new Response(JSON.stringify(body), { status }));
}

describe("the widget page's frame policy", () => {
  beforeEach(() => {
    clearFramePolicyCache();
  });

  it("is the backend's answer for that agent", async () => {
    const fetchImpl = answering({ frame_ancestors: "'self' https://shop.example.com" });

    expect(await frameAncestorsFor("ag_abc123", fetchImpl)).toBe("'self' https://shop.example.com");
    expect(fetchImpl).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/public\/embed\/ag_abc123\/frame-policy$/),
      expect.anything()
    );
  });

  it("admits nobody else when the backend cannot be reached", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error("connection refused");
    }) as unknown as typeof fetch;

    expect(await frameAncestorsFor("ag_abc123", fetchImpl)).toBe(FRAME_ANCESTORS_SELF);
  });

  it("admits nobody else when the backend answers with an error", async () => {
    expect(await frameAncestorsFor("ag_abc123", answering({ detail: "boom" }, 500))).toBe(
      FRAME_ANCESTORS_SELF
    );
  });

  it("does not put anything but source expressions into the header", async () => {
    const hostile = answering({
      frame_ancestors: "'self' https://ok.example; script-src * \r\nSet-Cookie: x=1",
    });

    expect(await frameAncestorsFor("ag_abc123", hostile)).toBe(FRAME_ANCESTORS_SELF);
    expect(safeFrameAncestors("'self' 'unsafe-inline'")).toBeNull();
    expect(safeFrameAncestors("")).toBeNull();
    expect(safeFrameAncestors(undefined)).toBeNull();
    expect(safeFrameAncestors("'self' https://*.partner.example http://localhost:*")).toBe(
      "'self' https://*.partner.example http://localhost:*"
    );
  });

  it("does not ask the backend about something that is not a public id", async () => {
    const fetchImpl = answering({ frame_ancestors: "*" });

    expect(await frameAncestorsFor("../../admin", fetchImpl)).toBe(FRAME_ANCESTORS_SELF);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("asks once a minute per agent, and again sooner after a failure", async () => {
    let clock = 0;
    const now = () => clock;
    const good = answering({ frame_ancestors: "'self' https://shop.example.com" });

    await frameAncestorsFor("ag_abc123", good, now);
    clock = 59_000;
    await frameAncestorsFor("ag_abc123", good, now);
    expect(good).toHaveBeenCalledTimes(1);
    clock = 61_000;
    await frameAncestorsFor("ag_abc123", good, now);
    expect(good).toHaveBeenCalledTimes(2);

    clearFramePolicyCache();
    clock = 0;
    const bad = answering({}, 503);
    await frameAncestorsFor("ag_abc123", bad, now);
    clock = 6_000;
    expect(await frameAncestorsFor("ag_abc123", good, now)).toBe("'self' https://shop.example.com");
  });
});
