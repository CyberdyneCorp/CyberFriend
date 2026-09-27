import { describe, expect, it } from "vitest";

import { draftOf, draftProblem, patchFor } from "./featureRequests";
import type { FeatureRequest } from "./types";

const ROW: FeatureRequest = {
  id: 12,
  text: "dark mode",
  language: "en",
  status: "new",
  admin_note: null,
  duplicate_of: null,
  source_kind: "command",
  person: "Sam",
  same_text_elsewhere: 0,
  created_at: "2026-09-15T10:00:00+00:00",
  updated_at: "2026-09-15T10:00:00+00:00",
  updated_by: null,
};

describe("patchFor", () => {
  it("sends nothing for an untouched draft", () => {
    expect(patchFor(ROW, draftOf(ROW))).toEqual({});
    const noted = { ...ROW, admin_note: "Q4", duplicate_of: 3 };
    expect(patchFor(noted, draftOf(noted))).toEqual({});
  });

  it("sends only what changed", () => {
    expect(patchFor(ROW, { ...draftOf(ROW), status: "planned" })).toEqual({ status: "planned" });
    expect(patchFor(ROW, { ...draftOf(ROW), note: "  Q4  " })).toEqual({ admin_note: "Q4" });
    expect(patchFor(ROW, { ...draftOf(ROW), duplicateOf: "3" })).toEqual({ duplicate_of: 3 });
  });

  it("clears a note or a link emptied in the form", () => {
    const noted = { ...ROW, admin_note: "Q4", duplicate_of: 3 };
    expect(patchFor(noted, { ...draftOf(noted), note: " ", duplicateOf: "" })).toEqual({
      admin_note: null,
      duplicate_of: null,
    });
  });
});

describe("draftProblem", () => {
  it("refuses a duplicate link that is not a number or is the row itself", () => {
    expect(draftProblem({ ...draftOf(ROW), duplicateOf: "abc" })).toMatch(/number/);
    expect(draftProblem({ ...draftOf(ROW), duplicateOf: "12" })).toMatch(/itself/);
    expect(draftProblem({ ...draftOf(ROW), duplicateOf: "3" })).toBeNull();
  });
});
