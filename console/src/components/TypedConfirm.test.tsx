// @vitest-environment jsdom
/**
 * The gate, exercised the way an operator meets it.
 *
 * `confirmationMatches` is unit-tested next door; this test exists because
 * the requirement is about the *button*. "A dialog somebody can click through
 * is not a confirmation" is a claim about what is clickable, and the way it
 * regresses is a well-meant `disabled={busy}` that drops the name check while
 * every other test stays green.
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TypedConfirm } from "./TypedConfirm";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  // React 18 renders outside act() with a warning unless this is set.
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function render(onConfirm: () => void, toolName = "restart_service"): void {
  act(() => {
    root.render(
      <TypedConfirm
        toolName={toolName}
        serverName="ops"
        tool={{ effect: "mutating", mutates: true }}
        busy={false}
        onConfirm={onConfirm}
        onCancel={() => undefined}
      />,
    );
  });
}

function enableButton(): HTMLButtonElement {
  const button = Array.from(host.querySelectorAll("button")).find((candidate) =>
    candidate.textContent?.includes("Enable this tool"),
  );
  if (button === undefined) throw new Error("the enable button is not rendered");
  return button as HTMLButtonElement;
}

function type(text: string): void {
  const input = host.querySelector("input");
  if (input === null) throw new Error("the confirmation input is not rendered");
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )?.set;
  act(() => {
    setter?.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("enabling a state-changing tool", () => {
  it("cannot be clicked through", () => {
    const onConfirm = vi.fn();
    render(onConfirm);

    expect(enableButton().disabled).toBe(true);
    act(() => {
      enableButton().click();
    });
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("stays shut for a name that is close but not the tool's", () => {
    const onConfirm = vi.fn();
    render(onConfirm);

    for (const near of ["restart", "Restart_Service", "restart_service_2"]) {
      type(near);
      expect(enableButton().disabled).toBe(true);
    }
    act(() => {
      enableButton().click();
    });
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("opens only once the tool's own name is typed", () => {
    const onConfirm = vi.fn();
    render(onConfirm);

    type("restart_service");
    expect(enableButton().disabled).toBe(false);
    act(() => {
      enableButton().click();
    });
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("names the tool and its effect where the operator is looking", () => {
    render(() => undefined);
    const text = host.textContent ?? "";
    expect(text).toContain("restart_service");
    expect(text).toContain("state-changing");
    expect(text).toContain("recorded as an escalation");
  });
});
