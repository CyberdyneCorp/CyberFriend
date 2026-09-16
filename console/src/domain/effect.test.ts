import { describe, expect, it } from "vitest";

import {
  confirmationMatches,
  effectLabel,
  effectOf,
  isStateChanging,
  toolEffect,
  toolMutates,
} from "./effect";

describe("effectOf", () => {
  it("treats declared read-only spellings as read-only", () => {
    for (const spelling of ["read_only", "read-only", "readonly", "READ", "  query "]) {
      expect(effectOf(spelling)).toBe("read-only");
    }
  });

  it("treats an undeclared effect as state-changing", () => {
    // The rule the API enforces, mirrored so the screen and the refusal agree.
    for (const undeclared of [undefined, null, "", "   "]) {
      expect(effectOf(undeclared)).toBe("state-changing");
      expect(isStateChanging(undeclared)).toBe(true);
    }
  });

  it("treats an unrecognised effect as state-changing", () => {
    // A new effect word must not ship as harmless by default.
    expect(effectOf("mutates")).toBe("state-changing");
    expect(effectOf("probably-fine")).toBe("state-changing");
  });

  it("says when state-changing is a guess rather than a declaration", () => {
    expect(effectLabel(null)).toBe("state-changing (undeclared)");
    expect(effectLabel("write")).toBe("state-changing");
    expect(effectLabel("read")).toBe("read-only");
  });
});

describe("confirmationMatches", () => {
  it("requires the exact tool name", () => {
    expect(confirmationMatches("send_email", "send_email")).toBe(true);
    expect(confirmationMatches(" send_email ", "send_email")).toBe(true);
  });

  it("refuses a near miss", () => {
    expect(confirmationMatches("Send_Email", "send_email")).toBe(false);
    expect(confirmationMatches("send", "send_email")).toBe(false);
    expect(confirmationMatches("send_email_2", "send_email")).toBe(false);
    expect(confirmationMatches("", "send_email")).toBe(false);
  });

  it("cannot be passed by confirming a nameless tool", () => {
    expect(confirmationMatches("", "")).toBe(false);
    expect(confirmationMatches("   ", "  ")).toBe(false);
  });
});

describe("toolEffect", () => {
  it("takes the API's own verdict when it sent one", () => {
    expect(toolEffect({ effect: "read_only", mutates: true })).toBe("state-changing");
    expect(toolEffect({ effect: "mutating", mutates: false })).toBe("read-only");
  });

  it("falls back to the effect word, failing closed", () => {
    expect(toolEffect({ effect: "read_only" })).toBe("read-only");
    expect(toolEffect({ effect: "undetermined" })).toBe("state-changing");
    expect(toolEffect({})).toBe("state-changing");
    expect(toolMutates({})).toBe(true);
  });

  it("distinguishes 'could not tell' from 'did not say' in words", () => {
    expect(effectLabel("undetermined")).toBe("state-changing (undetermined)");
    expect(effectLabel(undefined)).toBe("state-changing (undeclared)");
  });
});
