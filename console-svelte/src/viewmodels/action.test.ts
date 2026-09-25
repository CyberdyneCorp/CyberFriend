import { describe, expect, it } from "vitest";

import { Action } from "./action.svelte";

describe("Action", () => {
  it("keeps the API's note from a call that succeeded", async () => {
    const action = new Action();
    await action.run(async () => "added, but the bot cannot read it");
    expect(action.note).toBe("added, but the bot cannot read it");
    expect(action.error).toBeNull();
    expect(action.busy).toBe(false);
  });

  it("is busy while the work runs", async () => {
    const action = new Action();
    let finish!: () => void;
    const running = action.run(() => new Promise<void>((resolve) => (finish = resolve)));
    expect(action.busy).toBe(true);
    finish();
    await running;
    expect(action.busy).toBe(false);
    expect(action.note).toBeNull();
  });

  it("turns a failure into state rather than a rejection", async () => {
    const action = new Action();
    await action.run(async () => {
      throw new Error("the confirmation must name the tool being enabled");
    });
    expect(action.error).toBe("the confirmation must name the tool being enabled");
  });

  it("forgets the last outcome when the next run starts, and on clear", async () => {
    const action = new Action();
    await action.run(async () => {
      throw new Error("no");
    });
    await action.run(async () => "yes");
    expect(action.error).toBeNull();
    action.clear();
    expect(action.note).toBeNull();
  });
});
