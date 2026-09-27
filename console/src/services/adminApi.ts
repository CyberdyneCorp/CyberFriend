/**
 * The admin API, one call per route.
 *
 * Nothing here calls `fetch` directly: `http.ts` is the only file that does,
 * so the credential and the 401 rule live in one place. Signing in and out
 * is `sessionApi.ts`.
 */

import type {
  AllowlistEntry,
  AuditEntry,
  Changed,
  Channel,
  ChannelAdded,
  FederationServer,
  OptOut,
  ServerAdded,
  Setting,
  Status,
  TokenRecord,
  ToolAllowed,
} from "../domain/types";
import { apiPath, send } from "./http";

export const adminApi = {
  status: () => send<Status>("GET", apiPath("status")),

  settings: () => send<Setting[]>("GET", apiPath("settings")),
  /**
   * `value` is the operator's text, verbatim. The API parses it against the
   * setting's own type and records exactly what was typed; coercing it here
   * would put a different value in the change record from the one entered.
   */
  putSetting: (key: string, value: string) =>
    send<Setting>("PUT", apiPath("settings", key), { value }),

  servers: () => send<FederationServer[]>("GET", apiPath("federation", "servers")),
  addServer: (name: string, target: string) =>
    send<ServerAdded>("POST", apiPath("federation", "servers"), { name, target }),
  removeServer: (name: string) =>
    send<Changed>("DELETE", apiPath("federation", "servers", name)),

  allowlist: () => send<AllowlistEntry[]>("GET", apiPath("federation", "allowlist")),
  allow: (entry: {
    server: string;
    tool: string;
    read_only: boolean;
    confirm_tool_name?: string;
  }) => send<ToolAllowed>("POST", apiPath("federation", "allowlist"), entry),
  disallow: (server: string, tool: string) =>
    send<Changed>("DELETE", apiPath("federation", "allowlist", server, tool)),

  channels: () => send<Channel[]>("GET", apiPath("channels")),
  addChannel: (id: string) => send<ChannelAdded>("POST", apiPath("channels"), { id }),
  removeChannel: (id: string) => send<Changed>("DELETE", apiPath("channels", id)),

  optOuts: () => send<OptOut[]>("GET", apiPath("optouts")),
  addOptOut: (platform: string, platform_user_id: string) =>
    send<Changed>("POST", apiPath("optouts"), { platform, platform_user_id }),
  removeOptOut: (platform: string, id: string) =>
    send<Changed>("DELETE", apiPath("optouts", platform, id)),

  // Review and revoke only. There is no `issueToken`, and the API has no
  // route for one: minting an MCP credential grants a read of one Discord
  // account's whole view of the corpus, which is not an authority this
  // console has. It is done from a shell with `python -m
  // chatmemory.mcp.issue_token`.
  tokens: () => send<TokenRecord[]>("GET", apiPath("tokens")),
  revokeToken: (id: string) => send<Changed>("DELETE", apiPath("tokens", id)),

  audit: () => send<AuditEntry[]>("GET", apiPath("audit")),
};

/** What a view-model is handed: the real client, or a fake with the same shape. */
export type AdminApi = typeof adminApi;
