import { describe, expect, it } from "vitest";

import { Confirmation } from "./confirmation.svelte";

describe("Confirmation", () => {
  it("does nothing on the first click", () => {
    const confirmation = new Confirmation();
    let calls = 0;
    confirmation.confirm(() => (calls += 1));
    expect(calls).toBe(0);
  });

  it("does it on the second, and disarms", () => {
    const confirmation = new Confirmation();
    let calls = 0;
    confirmation.arm();
    confirmation.confirm(() => (calls += 1));
    expect(calls).toBe(1);
    expect(confirmation.armed).toBe(false);
    confirmation.confirm(() => (calls += 1));
    expect(calls).toBe(1);
  });

  it("can be put back without doing anything", () => {
    const confirmation = new Confirmation();
    let calls = 0;
    confirmation.arm();
    confirmation.disarm();
    confirmation.confirm(() => (calls += 1));
    expect(calls).toBe(0);
  });
});
