// @vitest-environment jsdom
/**
 * The navigation offers only what the signed-in role may open.
 *
 * `Router.visibleTo` is pinned in the router's tests; this pins that the
 * shell renders its answer rather than the whole table. Every real route is
 * `operator` and every real session is `admin` until the login change, so the
 * end-to-end test cannot tell the difference -- hence a table of our own.
 */

import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it } from "vitest";

import type { Role } from "../domain/roles";
import type { HashPort } from "../services/hashLocation";
import Blank from "../test/Blank.svelte";
import type { ConsoleVM } from "../viewmodels/console.svelte";
import { Router, type Route } from "../viewmodels/router.svelte";
import type { ScreenView } from "./routes";
import Shell from "./Shell.svelte";

const staticHash: HashPort = { read: () => "/status", replace: () => {}, listen: () => () => {} };

const TABLE: readonly Route<ScreenView>[] = [
  { path: "/status", title: "Status", view: Blank, minRole: "operator" },
  { path: "/tokens", title: "Tokens", view: Blank, minRole: "admin" },
];

/** Just what the shell reads from the console, for a given role. */
function consoleFor(role: Role): ConsoleVM {
  return {
    session: { role, signOut: () => {} },
    router: <V>(routes: readonly Route<V>[]) => new Router(routes, staticHash, "/status"),
  } as unknown as ConsoleVM;
}

afterEach(() => cleanup());

describe("the shell's navigation", () => {
  it("leaves out a route the role may not open", () => {
    render(Shell, { props: { app: consoleFor("operator"), routes: TABLE } });
    expect(screen.getByRole("link", { name: "Status" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Tokens" })).toBeNull();
  });

  it("offers it to a role that may", () => {
    render(Shell, { props: { app: consoleFor("admin"), routes: TABLE } });
    expect(screen.getByRole("link", { name: "Tokens" })).toBeTruthy();
  });
});
