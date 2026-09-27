import { describe, expect, it } from "vitest";

import { allows, roleOf } from "./roles";

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

describe("roleOf", () => {
  it("takes the highest role the API reported", () => {
    expect(roleOf(["operator", "admin"])).toBe("admin");
    expect(roleOf(["operator"])).toBe("operator");
  });

  it("gives no role for none, or for names it does not know", () => {
    expect(roleOf([])).toBeNull();
    expect(roleOf(["cyberfriend:admin", "owner"])).toBeNull();
  });
});
