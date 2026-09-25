import { describe, expect, it } from "vitest";

import type { AuditEntry, Status } from "../domain/types";
import { RECENT_CHANGES, StatusVM } from "./status.svelte";

const REPORT: Status = {
  status: "ok",
  embedding_backlog: 31,
  ingestion: { messages: 128394, per_channel: { general: 4 } },
  configuration: { settings_total: 15 },
};

function change(index: number): AuditEntry {
  return {
    operator: "ana",
    setting: `setting_${index}`,
    before: null,
    after: null,
    kind: "applied",
    at: "2026-09-16T07:00:00+00:00",
  };
}

function fakeApi(report: Status | Error, audit: AuditEntry[] = []) {
  return {
    status: async () => {
      if (report instanceof Error) throw report;
      return report;
    },
    audit: async () => audit,
  };
}

describe("StatusVM", () => {
  it("puts top-level numbers on the health card and gives each group its own", async () => {
    const vm = new StatusVM(fakeApi(REPORT));
    await vm.load();
    expect(vm.health.map(([key]) => key)).toEqual(["status", "embedding_backlog"]);
    expect(vm.groups.map((group) => group.name)).toEqual(["ingestion", "configuration"]);
    expect(vm.groups[0]?.metrics).toEqual([["messages", 128394]]);
    expect(vm.groups[0]?.subgroups).toEqual([
      { name: "per_channel", metrics: [["general", 4]] },
    ]);
  });

  it("shows a field the API added without a console release", async () => {
    const vm = new StatusVM(fakeApi({ ...REPORT, new_counter: 7 }));
    await vm.load();
    expect(vm.health).toContainEqual(["new_counter", 7]);
  });

  it("shows only the most recent changes", async () => {
    const audit = Array.from({ length: 12 }, (_, index) => change(index));
    const vm = new StatusVM(fakeApi(REPORT, audit));
    await vm.load();
    expect(vm.recent).toHaveLength(RECENT_CHANGES);
    expect(vm.recent[0]?.setting).toBe("setting_0");
  });

  it("reports a refused status request as a problem, not as an empty screen", async () => {
    const vm = new StatusVM(fakeApi(new Error("That credential was refused.")));
    await vm.load();
    expect(vm.report.error).toBe("That credential was refused.");
    expect(vm.health).toEqual([]);
    expect(vm.groups).toEqual([]);
  });
});
