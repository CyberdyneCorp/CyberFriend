// @vitest-environment jsdom
/**
 * The gate, exercised the way an operator meets it.
 *
 * `TypedConfirmVM` is tested without a DOM; this exists because the
 * requirement is about the *button*. "A confirmation somebody can click
 * through is not one" is a claim about what is clickable, and the way it
 * regresses is a well-meant `disabled={busy}` that drops the name check while
 * every other test stays green.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { tick } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TypedConfirmVM } from "../../viewmodels/federation.svelte";
import TypedConfirm from "./TypedConfirm.svelte";

afterEach(() => cleanup());

function show(onConfirm: () => void): void {
  const gate = new TypedConfirmVM({
    server: "ops",
    tool: { name: "restart_service", effect: "mutating", mutates: true },
  });
  render(TypedConfirm, { props: { gate, busy: false, onConfirm, onCancel: () => {} } });
}

const enable = () => screen.getByRole("button", { name: "Enable this tool" }) as HTMLButtonElement;
/** `click()`, not `fireEvent.click`: like a browser, it does nothing on a disabled button. */
async function press(button: HTMLButtonElement): Promise<void> {
  button.click();
  await tick();
}

const type = (text: string) => fireEvent.input(screen.getByRole("textbox"), { target: { value: text } });

describe("enabling a state-changing tool", () => {
  it("cannot be clicked through", async () => {
    const onConfirm = vi.fn();
    show(onConfirm);
    expect(enable().disabled).toBe(true);
    await press(enable());
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("stays shut for a name that is close but not the tool's", async () => {
    const onConfirm = vi.fn();
    show(onConfirm);
    for (const near of ["restart", "Restart_Service", "restart_service_2"]) {
      await type(near);
      expect(enable().disabled).toBe(true);
    }
    await press(enable());
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("opens only once the tool's own name is typed", async () => {
    const onConfirm = vi.fn();
    show(onConfirm);
    await type("restart_service");
    expect(enable().disabled).toBe(false);
    await press(enable());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("names the tool and its effect where the operator is looking", () => {
    show(() => {});
    const text = document.body.textContent ?? "";
    expect(text).toContain("restart_service");
    expect(text).toContain("state-changing");
    expect(text).toContain("recorded as an escalation");
  });
});
