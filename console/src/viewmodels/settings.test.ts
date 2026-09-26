import { describe, expect, it } from "vitest";

import type { Setting } from "../domain/types";
import { SAVED, STILL_OVERRIDDEN, SettingsVM, savedNote } from "./settings.svelte";

const ROWS: Setting[] = [
  { key: "window_max_tokens", value: 1200, text: "1200", source: "environment", editable: true },
];

function fakeApi(saveAs: Setting["source"] | Error = "database") {
  const puts: [string, string][] = [];
  let reads = 0;
  return {
    puts,
    reads: () => reads,
    settings: async () => {
      reads += 1;
      return ROWS;
    },
    putSetting: async (key: string, value: string): Promise<Setting> => {
      puts.push([key, value]);
      if (saveAs instanceof Error) throw saveAs;
      return { key, value, source: saveAs, editable: true };
    },
  };
}

describe("savedNote", () => {
  it("says the environment still wins when the API says so, in either spelling", () => {
    expect(savedNote({ source: "environment" })).toBe(STILL_OVERRIDDEN);
    expect(savedNote({ source: "env" })).toBe(STILL_OVERRIDDEN);
    expect(savedNote({ source: "database" })).toBe(SAVED);
  });
});

describe("SettingsVM", () => {
  it("edits from the operator's text and sends it verbatim", async () => {
    const api = fakeApi();
    const vm = new SettingsVM(api);
    await vm.load();
    const editor = vm.editor("window_max_tokens");
    editor.edit(ROWS[0]!);
    expect(editor.typed).toBe("1200");
    editor.typed = " 1500 ";
    await editor.save();
    expect(api.puts).toEqual([["window_max_tokens", " 1500 "]]);
    expect(editor.typed).toBeNull();
    expect(editor.action.note).toBe(SAVED);
    // The list is read again, so the new provenance shows.
    expect(api.reads()).toBe(2);
  });

  it("says a save the environment still overrides is not in force", async () => {
    const vm = new SettingsVM(fakeApi("environment"));
    const editor = vm.editor("window_max_tokens");
    editor.edit(ROWS[0]!);
    await editor.save();
    expect(editor.action.note).toBe(STILL_OVERRIDDEN);
  });

  it("keeps the edit box open with the refusal when the API refuses", async () => {
    const vm = new SettingsVM(fakeApi(new Error("expected a number")));
    const editor = vm.editor("window_max_tokens");
    editor.edit(ROWS[0]!);
    await editor.save();
    expect(editor.action.error).toBe("expected a number");
    expect(editor.typed).toBe("1200");
  });

  it("sends nothing when the row is not being edited, and cancel forgets the text", async () => {
    const api = fakeApi();
    const vm = new SettingsVM(api);
    const editor = vm.editor("window_max_tokens");
    await editor.save();
    editor.edit(ROWS[0]!);
    editor.cancel();
    await editor.save();
    expect(api.puts).toEqual([]);
  });

  it("hands out one editor per key", () => {
    const vm = new SettingsVM(fakeApi());
    expect(vm.editor("a")).toBe(vm.editor("a"));
    expect(vm.editor("a")).not.toBe(vm.editor("b"));
  });
});
