// @vitest-environment jsdom
/**
 * The Svelte console, driven end to end against the stub API.
 *
 * The view-model tests pin the rules; this pins that they are wired to the
 * screen. It is the template the other screens' scenarios follow when they
 * are ported: real services, real views, and the `node:http` stub in
 * `test/stubApi.ts`, so it needs no database, Discord token or model key.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { adminApi } from "./services/adminApi";
import { browserHash } from "./services/hashLocation";
import { session, signOut } from "./services/session";
import { startStubApi, TOKEN, type StubApi } from "./test/stubApi";
import { ConsoleVM } from "./viewmodels/console.svelte";
import App from "./views/App.svelte";

let api: StubApi;
const realFetch = globalThis.fetch;

beforeEach(async () => {
  // jsdom keeps one window for the whole file, and the router reads the hash
  // from it, so a test would otherwise start where the last one left off.
  window.location.hash = "";
  api = await startStubApi();
  // The console asks for "/api/...", which jsdom resolves against its own origin.
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
    realFetch(new URL(String(input), api.origin), init)) as typeof fetch;
  render(App, { props: { app: new ConsoleVM(adminApi, session, browserHash) } });
});

afterEach(async () => {
  cleanup();
  signOut();
  globalThis.fetch = realFetch;
  await api.close();
});

async function signIn(credential: string): Promise<void> {
  await fireEvent.input(screen.getByPlaceholderText("cfa_…"), { target: { value: credential } });
  await fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("signing in", () => {
  it("says so when the credential is refused, and stays on the form", async () => {
    await signIn("cfa_not_a_real_token");
    await screen.findByText("That credential was refused.");
    expect(screen.getByPlaceholderText("cfa_…")).toBeTruthy();
    expect(screen.queryByText("Ingestion")).toBeNull();
  });

  it("opens the status screen with a credential that works", async () => {
    await signIn(TOKEN);
    await screen.findByText("Ingestion");
    expect(screen.getByText("128,394")).toBeTruthy();
    // The unknown (empty) address was sent to the status screen.
    expect(window.location.hash).toBe("#/status");
  });
});

describe("the status screen", () => {
  beforeEach(async () => {
    await signIn(TOKEN);
    await screen.findByText("Ingestion");
  });

  it("lists the recent configuration changes, refusals included", async () => {
    await screen.findByText("discord_token");
    expect(screen.getByText("refused").className).toContain("badge--kind-refused");
  });

  it("goes back to the status screen from an address that names no screen", async () => {
    window.location.hash = "#/federation";
    await screen.findByText("Not ported yet");
    window.location.hash = "#/no-such-screen";
    await screen.findByText("Ingestion");
    await waitFor(() => expect(window.location.hash).toBe("#/status"));
  });

  it("forgets the credential and the screens on sign-out", async () => {
    await fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByPlaceholderText("cfa_…");
    expect(screen.queryByText("Ingestion")).toBeNull();
  });
});
