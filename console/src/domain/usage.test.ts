import { describe, expect, it } from "vitest";

import { formatCost, formatCount, formatSeconds, lastDays, windowProblem } from "./usage";

const TODAY = Date.parse("2026-09-26T15:00:00Z");

describe("the usage window", () => {
  it("ends today and counts today as one of its days", () => {
    expect(lastDays(30, TODAY)).toEqual({ from: "2026-08-28", to: "2026-09-26" });
    expect(lastDays(1, TODAY)).toEqual({ from: "2026-09-26", to: "2026-09-26" });
  });

  it("accepts exactly 90 days and refuses 91, as the API does", () => {
    expect(windowProblem(lastDays(90, TODAY))).toBeNull();
    expect(windowProblem(lastDays(91, TODAY))).toBe("The window is at most 90 days.");
  });

  it("refuses a reversed or missing window before asking", () => {
    expect(windowProblem({ from: "2026-09-10", to: "2026-09-01" })).toBe(
      "The start must not be after the end.",
    );
    expect(windowProblem({ from: "", to: "2026-09-01" })).toBe("Pick both dates.");
    expect(windowProblem({ from: "2026-02-30", to: "2026-03-01" })).toBe("Pick both dates.");
  });
});

describe("usage figures", () => {
  it("shows a figure that does not apply as a dash, not a zero", () => {
    expect(formatCount(null)).toBe("—");
    expect(formatCount(0)).toBe("0");
    expect(formatCost(null)).toBe("—");
    expect(formatSeconds(null)).toBe("—");
  });

  it("keeps a small cost visible", () => {
    expect(formatCost(0.0012)).toBe("$0.0012");
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(12.5)).toBe("$12.50");
  });

  it("reads voice time in minutes", () => {
    expect(formatSeconds(45)).toBe("45 s");
    expect(formatSeconds(600)).toBe("10 min");
  });
});
