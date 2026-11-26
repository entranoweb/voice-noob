import { describe, expect, it } from "vitest";

import { embedTokenHeaders } from "./embed-token-request";

describe("embedTokenHeaders", () => {
  it("sends the session token when the widget holds one", () => {
    const headers = embedTokenHeaders("abc.def", "https://shop.example.com", {});
    expect(headers["X-Embed-Session"]).toBe("abc.def");
    expect(headers.Origin).toBe("https://shop.example.com");
  });

  it("sends no session header for a first session", () => {
    const headers = embedTokenHeaders("", "https://shop.example.com", {});
    expect("X-Embed-Session" in headers).toBe(false);
  });

  it("keeps the parent origin header", () => {
    const headers = embedTokenHeaders("t", "https://a.example", {
      "X-Embed-Parent-Origin": "https://b.example",
    });
    expect(headers["X-Embed-Parent-Origin"]).toBe("https://b.example");
  });
});
