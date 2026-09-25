import { describe, expect, it } from "vitest";

import { isRetentionSetting, settingText, sourceOf } from "./settings";

describe("sourceOf", () => {
  it("understands both spellings the API and the contract use", () => {
    expect(sourceOf("db")).toBe("database");
    expect(sourceOf("database")).toBe("database");
    expect(sourceOf("env")).toBe("environment");
    expect(sourceOf("environment")).toBe("environment");
    expect(sourceOf("default")).toBe("default");
  });

  it("says unknown rather than guessing", () => {
    // Claiming a value came from the database when nobody knows is exactly
    // the failure this column exists to prevent.
    expect(sourceOf("somewhere")).toBe("unknown");
    expect(sourceOf("")).toBe("unknown");
  });
});

describe("settingText", () => {
  it("prefers the API's own rendering of the value", () => {
    expect(settingText({ value: ["100", "200"], text: "100 200" })).toBe("100 200");
  });

  it("falls back to a typeable rendering when there is none", () => {
    expect(settingText({ value: ["100", "200"] })).toBe("100 200");
    expect(settingText({ value: true })).toBe("true");
    expect(settingText({ value: 180 })).toBe("180");
    expect(settingText({ value: null })).toBe("");
  });
});

describe("isRetentionSetting", () => {
  it("picks out what an operator reads as retention", () => {
    expect(isRetentionSetting("retention_days")).toBe(true);
    expect(isRetentionSetting("window_max_tokens")).toBe(false);
  });
});
