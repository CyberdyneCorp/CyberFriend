/**
 * Every screen, in navigation order, with the role it needs.
 *
 * `minRole` has no default: a route without one does not type-check, the same
 * rule as the server's route table. Every screen here reads, so an operator
 * may open all of them; the controls that change something are the server's
 * to refuse and, after the login change, the view's to hide.
 */

import type { Component } from "svelte";

import type { ConsoleVM } from "../viewmodels/console.svelte";
import type { Route } from "../viewmodels/router.svelte";
import NotPorted from "./screens/NotPorted.svelte";
import StatusScreen from "./screens/StatusScreen.svelte";

export type ScreenView = Component<{ app: ConsoleVM }>;

export const ROUTES: readonly Route<ScreenView>[] = [
  { path: "/status", title: "Status", view: StatusScreen, minRole: "operator" },
  { path: "/federation", title: "Federation", view: NotPorted, minRole: "operator" },
  { path: "/channels", title: "Channels", view: NotPorted, minRole: "operator" },
  { path: "/retention", title: "Retention", view: NotPorted, minRole: "operator" },
  { path: "/settings", title: "Settings", view: NotPorted, minRole: "operator" },
  { path: "/tokens", title: "Tokens", view: NotPorted, minRole: "operator" },
  { path: "/audit", title: "Audit", view: NotPorted, minRole: "operator" },
];
