import { describe, it, expect } from "vitest";
import { metadata } from "../layout";

describe("reset-password layout", () => {
  it("sends no Referer, since the URL carries the reset secret", () => {
    expect(metadata.referrer).toBe("no-referrer");
  });
});
