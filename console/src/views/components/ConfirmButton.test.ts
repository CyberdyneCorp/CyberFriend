// @vitest-environment jsdom
/** A removal is sent only on a second, deliberate click, and "Keep" sends nothing. */

import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import ConfirmButton from "./ConfirmButton.svelte";

afterEach(() => cleanup());

function show(onConfirm: () => void): void {
  render(ConfirmButton, { props: { label: "Revoke", confirmLabel: "Revoke this token", onConfirm } });
}

describe("a two-click removal", () => {
  it("names the consequence on the first click and acts on the second", async () => {
    const onConfirm = vi.fn();
    show(onConfirm);
    await fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    expect(onConfirm).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Revoke this token" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Revoke" })).toBeTruthy();
  });

  it("sends nothing when the operator keeps it", async () => {
    const onConfirm = vi.fn();
    show(onConfirm);
    await fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Revoke this token" })).toBeNull();
  });
});
