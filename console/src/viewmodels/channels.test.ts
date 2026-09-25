import { describe, expect, it } from "vitest";

import type { Channel, ChannelAdded, Changed } from "../domain/types";
import { ChannelsVM, addedNote } from "./channels.svelte";

const GENERAL: Channel = { id: "100", name: "general", indexed: true, readable_by_bot: true };

function fakeApi(added: Partial<Channel> = {}, detail?: string) {
  const calls: string[] = [];
  return {
    calls,
    channels: async () => [GENERAL],
    addChannel: async (id: string): Promise<ChannelAdded> => {
      calls.push(`add ${id}`);
      return { changed: "indexed_channel_ids", channel: { ...GENERAL, id, ...added }, ...(detail ? { detail } : {}) };
    },
    removeChannel: async (id: string): Promise<Changed> => {
      calls.push(`remove ${id}`);
      return { changed: "indexed_channel_ids" };
    },
  };
}

describe("addedNote", () => {
  it("says out loud that an added channel the bot cannot read will index nothing", () => {
    const note = addedNote({
      changed: "x",
      channel: { id: "200", name: "", indexed: true, readable_by_bot: false },
    });
    expect(note).toContain("200 is now in scope");
    expect(note).toContain("the bot cannot read it");
  });

  it("uses the API's own sentence when it wrote one", () => {
    expect(addedNote({ changed: "x", detail: "picked up at the next refresh", channel: GENERAL })).toBe(
      "picked up at the next refresh",
    );
  });
});

describe("ChannelsVM", () => {
  it("adds the trimmed id, clears the form and reports", async () => {
    const api = fakeApi({ readable_by_bot: false });
    const vm = new ChannelsVM(api);
    vm.id = " 300 \n";
    await vm.add();
    expect(api.calls).toEqual(["add 300"]);
    expect(vm.id).toBe("");
    expect(vm.adding.note).toContain("cannot read");
  });

  it("will not add an empty id", async () => {
    const api = fakeApi();
    const vm = new ChannelsVM(api);
    vm.id = "  ";
    expect(vm.canAdd).toBe(false);
    await vm.add();
    expect(api.calls).toEqual([]);
  });

  it("removes a channel and names it", async () => {
    const api = fakeApi();
    const vm = new ChannelsVM(api);
    await vm.remove(GENERAL);
    expect(api.calls).toEqual(["remove 100"]);
    expect(vm.action.note).toBe("general is out of scope.");
  });
});
