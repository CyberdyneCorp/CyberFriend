import { describe, expect, it } from "vitest";

import { allowlistKeys, isAllowlisted, toolKey } from "./allowlist";

describe("allowlist keys", () => {
  it("keys an entry by server and tool", () => {
    const keys = allowlistKeys([{ server: "ops", tool: "restart", mutation_enabled: true }]);
    expect([...keys]).toEqual([toolKey("ops", "restart")]);
    expect(allowlistKeys(null).size).toBe(0);
  });

  it("prefers the server's own flag, and falls back to the allowlist", () => {
    const keys = allowlistKeys([{ server: "ops", tool: "restart", mutation_enabled: false }]);
    expect(isAllowlisted("ops", { name: "restart" }, keys)).toBe(true);
    expect(isAllowlisted("ops", { name: "restart", allowlisted: false }, keys)).toBe(false);
    expect(isAllowlisted("wiki", { name: "restart" }, keys)).toBe(false);
    expect(isAllowlisted("wiki", { name: "search", allowlisted: true }, keys)).toBe(true);
  });
});
