import { describe, expect, it } from "vitest";

import type { OptOut, Setting } from "../domain/types";
import { OPTED_OUT, RetentionVM } from "./retention.svelte";

const SETTINGS: Setting[] = [
  { key: "retention_days", value: 180, source: "default", editable: true },
  { key: "window_max_tokens", value: 1200, source: "environment", editable: true },
];

function fakeApi(optOuts: OptOut[]) {
  const calls: string[] = [];
  return {
    calls,
    settings: async () => SETTINGS,
    putSetting: async (key: string, value: string): Promise<Setting> => ({
      key,
      value,
      source: "database",
      editable: true,
    }),
    optOuts: async () => optOuts,
    addOptOut: async (platform: string, id: string) => {
      calls.push(`add ${platform}:${id}`);
      return { changed: "opt_out" };
    },
    removeOptOut: async (platform: string, id: string) => {
      calls.push(`remove ${platform}:${id}`);
      return { changed: "opt_out" };
    },
  };
}

describe("RetentionVM", () => {
  it("shows only the retention settings", async () => {
    const vm = new RetentionVM(fakeApi([]));
    await vm.load();
    expect(vm.retention.map((s) => s.key)).toEqual(["retention_days"]);
  });

  it("opts a person out, keeping the platform and clearing the id", async () => {
    const api = fakeApi([]);
    const vm = new RetentionVM(api);
    vm.userId = " 42 ";
    await vm.add();
    expect(api.calls).toEqual(["add discord:42"]);
    expect(vm.userId).toBe("");
    expect(vm.platform).toBe("discord");
    expect(vm.adding.note).toBe(OPTED_OUT);
  });

  it("opts back in by platform and id, split on the last colon", async () => {
    const api = fakeApi([{ person: "discord:42", since: "2026-08-01" }]);
    const vm = new RetentionVM(api);
    await vm.load();
    await vm.optBackIn(vm.rows[0]!);
    expect(api.calls).toEqual(["remove discord:42"]);
    expect(vm.action.note).toBe("discord:42 is recorded again from now on.");
  });

  it("never guesses a person it cannot read", async () => {
    const api = fakeApi([{ person: "not a person", since: "2026-08-01" }]);
    const vm = new RetentionVM(api);
    await vm.load();
    expect(vm.rows[0]?.person).toBeNull();
    await vm.optBackIn(vm.rows[0]!);
    expect(api.calls).toEqual([]);
  });
});
