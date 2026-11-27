import { describe, expect, it } from "vitest";

import { NON_EMBED_SOURCE, securityHeaders } from "./security-headers";

// Next.js compiles `source` with path-to-regexp; the parenthesised group is a
// plain regular expression matched against the whole path.
const pattern = new RegExp(`^${NON_EMBED_SOURCE.replace(/^\/\((.*)\)$/, "/$1")}$`);
const matches = (path: string): boolean => pattern.test(path);
const names = (production: boolean): string[] =>
  (securityHeaders(production)[0]?.headers ?? []).map((h) => h.key);

describe("securityHeaders", () => {
  it("covers dashboard and API-proxy paths", () => {
    for (const path of ["/", "/dashboard", "/dashboard/agents/1", "/api/v1/agents", "/embedding"]) {
      expect(matches(path), path).toBe(true);
    }
  });

  it("never matches the embeddable widget, which customer sites must be able to frame", () => {
    for (const path of ["/embed", "/embed/abc123", "/embed/abc123/preview"]) {
      expect(matches(path), path).toBe(false);
    }
  });

  it("forbids framing and sniffing", () => {
    const h = Object.fromEntries(
      (securityHeaders(true)[0]?.headers ?? []).map((x) => [x.key, x.value])
    );
    expect(h["X-Frame-Options"]).toBe("DENY");
    expect(h["Content-Security-Policy"]).toBe("frame-ancestors 'none'");
    expect(h["X-Content-Type-Options"]).toBe("nosniff");
  });

  it("sends HSTS in production only, and never X-XSS-Protection", () => {
    expect(names(true)).toContain("Strict-Transport-Security");
    expect(names(false)).not.toContain("Strict-Transport-Security");
    expect(names(true)).not.toContain("X-XSS-Protection");
  });
});
