// @vitest-environment jsdom
/**
 * The console, driven end to end against a stub API.
 *
 * The unit tests pin the rules; this one pins that the rules are *wired to
 * the screen*. It is here because the bug it first caught could not have been
 * found any other way: the sign-in screen used to put the token in the
 * session and check it afterwards, so a refused credential unmounted the form
 * mid-request and the operator got a blank box and no message. Every piece
 * was individually correct.
 *
 * It carries its own server (`test/fixtures.ts`), so it needs no database, no
 * Discord token and no model key.
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { App } from "./App";
import { signOut } from "./auth/session";
import { startStubApi, TOKEN, type StubApi } from "./test/fixtures";

let api: StubApi;
let host: HTMLDivElement;
let root: Root;
const realFetch = globalThis.fetch;

beforeEach(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  // jsdom keeps one window for the whole file, and the router reads the hash
  // from it, so a test would otherwise start on whatever screen the last one
  // navigated to.
  window.location.hash = "";
  api = await startStubApi();
  // The app asks for "/api/...", which jsdom resolves against its own origin.
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
    realFetch(new URL(String(input), api.origin), init)) as typeof fetch;
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root.render(<App />);
  });
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  signOut();
  globalThis.fetch = realFetch;
  await api.close();
});

/** Wait for the screen to say something, rather than for a fixed delay. */
async function until(predicate: () => boolean, what: string): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
  throw new Error(`timed out waiting for ${what}; screen reads:\n${host.textContent}`);
}

const shows = (text: string) => () => (host.textContent ?? "").includes(text);

function element<T extends Element>(selector: string, text: string): T {
  const found = Array.from(host.querySelectorAll(selector)).find((candidate) =>
    candidate.textContent?.includes(text),
  );
  if (found === undefined) throw new Error(`no ${selector} reading ${JSON.stringify(text)}`);
  return found as T;
}

function typeInto(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  act(() => {
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function click(selector: string, text: string): Promise<void> {
  await act(async () => {
    element<HTMLElement>(selector, text).click();
  });
}

async function signIn(credential: string): Promise<void> {
  typeInto(host.querySelector("input") as HTMLInputElement, credential);
  await click("button", "Sign in");
}

describe("signing in", () => {
  it("says so when the credential is refused, and stays on the form", async () => {
    await signIn("cfa_not_a_real_token");
    await until(shows("refused"), "the refusal");
    // The screen the operator is left on is the one they can act on.
    expect(host.querySelector("input")).not.toBeNull();
    expect(shows("Status")()).toBe(false);
  });

  it("opens the console with a credential that works", async () => {
    await signIn(TOKEN);
    await until(shows("Ingestion"), "the status screen");
    expect(shows("128,394")()).toBe(true);
  });
});

describe("the federation screen", () => {
  beforeEach(async () => {
    await signIn(TOKEN);
    await until(shows("Ingestion"), "the status screen");
    await click("a", "Federation");
    await until(shows("restart_service"), "the servers");
  });

  it("marks a state-changing tool differently from a read-only one", () => {
    const mutating = element<HTMLElement>("li.tool", "restart_service");
    const readOnly = element<HTMLElement>("li.tool", "search");
    expect(mutating.className).toContain("tool--mutating");
    expect(readOnly.className).not.toContain("tool--mutating");
    expect(mutating.textContent).toContain("state-changing");
    // A server's own claim does not soften the badge.
    expect(element<HTMLElement>("li.tool", "page_oncall").textContent).toContain(
      "state-changing (undetermined)",
    );
  });

  it("allowlists a read-only tool without a confirmation", async () => {
    await act(async () => {
      element<HTMLElement>("li.tool", "search").querySelector("button")?.click();
    });
    await until(shows("is now available to the agent"), "the result");
    expect(api.allowCalls).toEqual([
      { server: "wikipedia", tool: "search", read_only: true },
    ]);
  });

  it("will not enable a state-changing tool until its name is typed", async () => {
    await act(async () => {
      element<HTMLElement>("li.tool", "restart_service").querySelector("button")?.click();
    });
    await until(shows("Type"), "the confirmation");

    const enable = () => element<HTMLButtonElement>("button", "Enable this tool");
    expect(enable().disabled).toBe(true);
    await act(async () => enable().click());
    expect(api.allowCalls).toEqual([]);

    typeInto(host.querySelector(".confirm input") as HTMLInputElement, "restart");
    expect(enable().disabled).toBe(true);

    typeInto(host.querySelector(".confirm input") as HTMLInputElement, "restart_service");
    expect(enable().disabled).toBe(false);
    await act(async () => enable().click());
    await until(shows("escalation is recorded"), "the escalation");
    expect(api.allowCalls).toEqual([
      {
        server: "ops",
        tool: "restart_service",
        read_only: false,
        confirm_tool_name: "restart_service",
      },
    ]);
  });
});

describe("the other screens", () => {
  beforeEach(async () => {
    await signIn(TOKEN);
    await until(shows("Ingestion"), "the status screen");
  });

  it("says whether the bot can read each indexed channel", async () => {
    await click("a", "Channels");
    await until(shows("leadership"), "the channels");
    expect(shows("cannot read")()).toBe(true);
    expect(shows("the bot has never read a message here")()).toBe(true);
  });

  it("says where every setting's value came from", async () => {
    await click("a", "Settings");
    await until(shows("web_tools_enabled"), "the settings");
    const badges = Array.from(host.querySelectorAll(".badge"))
      .map((badge) => badge.textContent)
      .filter((text) => text === "database" || text === "environment" || text === "default");
    // One per row, not one for the table: a row without provenance is a row
    // an operator cannot act on.
    expect(badges.length).toBe(host.querySelectorAll("tbody tr").length);
    expect(shows("managed at /api/channels")()).toBe(true);
  });

  it("shows the record of what has been refused", async () => {
    await click("a", "Audit");
    await until(shows("discord_token"), "the audit");
    expect(shows("credentials are read from the environment")()).toBe(true);
    expect(element<HTMLElement>(".badge", "refused").className).toContain("kind-refused");
  });
});
