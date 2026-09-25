import { describe, expect, it } from "vitest";

import { allows } from "./roles";

describe("allows", () => {
  it("lets admin do what operator may", () => {
    expect(allows("admin", "operator")).toBe(true);
    expect(allows("admin", "admin")).toBe(true);
    expect(allows("operator", "operator")).toBe(true);
  });

  it("refuses a lower role and no role at all", () => {
    expect(allows("operator", "admin")).toBe(false);
    expect(allows(null, "operator")).toBe(false);
  });
});
