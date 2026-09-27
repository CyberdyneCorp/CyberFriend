import { describe, expect, it } from "vitest";

import { detailOf } from "./changed";

describe("detailOf", () => {
  it("prefers the handler's own sentence", () => {
    expect(detailOf({ changed: "x", detail: "picked up at the next refresh" }, "done")).toBe(
      "picked up at the next refresh",
    );
  });

  it("falls back when the API wrote nothing useful", () => {
    expect(detailOf({ changed: "x" }, "done")).toBe("done");
    expect(detailOf({ changed: "x", detail: "  " }, "done")).toBe("done");
  });
});
