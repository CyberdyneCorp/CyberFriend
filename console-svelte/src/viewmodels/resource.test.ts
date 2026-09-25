import { describe, expect, it } from "vitest";

import { Resource } from "./resource.svelte";

/** A promise the test settles by hand, to order two requests. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

describe("Resource", () => {
  it("starts as loading, so a screen never shows 'nothing' before it asked", () => {
    const resource = new Resource(async () => [1]);
    expect(resource.loading).toBe(true);
    expect(resource.data).toBeNull();
  });

  it("holds what arrived", async () => {
    const resource = new Resource(async () => ["general"]);
    await resource.reload();
    expect(resource.data).toEqual(["general"]);
    expect(resource.error).toBeNull();
    expect(resource.loading).toBe(false);
  });

  it("keeps a refusal apart from an empty answer", async () => {
    const resource = new Resource<string[]>(async () => {
      throw new Error("That credential was refused.");
    });
    await resource.reload();
    expect(resource.error).toBe("That credential was refused.");
    expect(resource.data).toBeNull();
    expect(resource.loading).toBe(false);
  });

  it("clears an earlier error once a reload works", async () => {
    let fail = true;
    const resource = new Resource(async () => {
      if (fail) throw new Error("down");
      return 1;
    });
    await resource.reload();
    fail = false;
    await resource.reload();
    expect(resource.error).toBeNull();
    expect(resource.data).toBe(1);
  });

  it("drops an older request that finishes after a newer one", async () => {
    const answers = [deferred<string>(), deferred<string>()];
    let call = 0;
    const resource = new Resource(() => answers[call++]!.promise);
    const first = resource.reload();
    const second = resource.reload();
    answers[1]!.resolve("new");
    await second;
    answers[0]!.resolve("old");
    await first;
    expect(resource.data).toBe("new");
    expect(resource.loading).toBe(false);
  });
});
