import { describe, expect, it } from "vitest";

import { formatRelative, formatValue, humanise, partition } from "./format";

const NOW = Date.parse("2026-09-16T12:00:00Z");
const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString();

describe("formatRelative", () => {
  it("says how long ago, in the largest unit that fits", () => {
    expect(formatRelative(ago(5), NOW)).toBe("just now");
    expect(formatRelative(ago(60), NOW)).toBe("1 minute ago");
    expect(formatRelative(ago(7200), NOW)).toBe("2 hours ago");
    expect(formatRelative(ago(86400 * 3), NOW)).toBe("3 days ago");
  });

  it("says nothing rather than something wrong", () => {
    expect(formatRelative(null, NOW)).toBe("—");
    expect(formatRelative("never", NOW)).toBe("—");
  });
});

describe("formatValue", () => {
  it("renders scalars as an operator reads them", () => {
    expect(formatValue(true)).toBe("yes");
    expect(formatValue(128394)).toBe("128,394");
    expect(formatValue(null)).toBe("—");
    expect(formatValue([])).toBe("—");
  });
});

describe("partition", () => {
  it("separates the numbers a card shows from the groups that get their own", () => {
    const { scalars, groups } = partition({
      status: "ok",
      embedding_backlog: 31,
      ingestion: { messages: 4 },
    });
    expect(scalars.map(([k]) => k)).toEqual(["status", "embedding_backlog"]);
    expect(groups.map(([k]) => k)).toEqual(["ingestion"]);
  });

  it("treats null as a value, not as a group", () => {
    // `ingestion: null` is the API saying it could not tell, and it belongs on
    // the screen as a dash rather than as an empty card.
    expect(partition({ ingestion: null }).scalars).toEqual([["ingestion", null]]);
  });
});

describe("humanise", () => {
  it("turns a field name into a label", () => {
    expect(humanise("channels_pending_rebuild")).toBe("Channels pending rebuild");
  });
});
