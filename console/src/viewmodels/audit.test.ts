import { describe, expect, it } from "vitest";

import type { AuditEntry, ChangeKind } from "../domain/types";
import { AuditVM } from "./audit.svelte";

function entry(setting: string, kind: ChangeKind): AuditEntry {
  return { operator: "ana", setting, before: null, after: null, kind, at: "2026-09-16T07:00:00+00:00" };
}

const ENTRIES = [entry("a", "applied"), entry("b", "escalation"), entry("c", "refused")];

describe("AuditVM", () => {
  it("shows everything until a kind is picked, then only that kind", async () => {
    const vm = new AuditVM({ audit: async () => ENTRIES });
    await vm.load();
    expect(vm.shown).toHaveLength(3);
    vm.show("escalation");
    expect(vm.shown.map((e) => e.setting)).toEqual(["b"]);
    vm.show("all");
    expect(vm.shown).toHaveLength(3);
  });

  it("shows nothing, not an error, before the record arrives", () => {
    const vm = new AuditVM({ audit: async () => ENTRIES });
    expect(vm.shown).toEqual([]);
    expect(vm.audit.error).toBeNull();
  });
});
