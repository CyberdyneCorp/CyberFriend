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
import AuditScreen from "./screens/AuditScreen.svelte";
import ChannelsScreen from "./screens/ChannelsScreen.svelte";
import FederationScreen from "./screens/FederationScreen.svelte";
import RetentionScreen from "./screens/RetentionScreen.svelte";
import SettingsScreen from "./screens/SettingsScreen.svelte";
import StatusScreen from "./screens/StatusScreen.svelte";
import TokensScreen from "./screens/TokensScreen.svelte";

export type ScreenView = Component<{ app: ConsoleVM }>;

export const ROUTES: readonly Route<ScreenView>[] = [
  { path: "/status", title: "Status", view: StatusScreen, minRole: "operator" },
  { path: "/federation", title: "Federation", view: FederationScreen, minRole: "operator" },
  { path: "/channels", title: "Channels", view: ChannelsScreen, minRole: "operator" },
  { path: "/retention", title: "Retention", view: RetentionScreen, minRole: "operator" },
  { path: "/settings", title: "Settings", view: SettingsScreen, minRole: "operator" },
  { path: "/tokens", title: "Tokens", view: TokensScreen, minRole: "operator" },
  { path: "/audit", title: "Audit", view: AuditScreen, minRole: "operator" },
];
