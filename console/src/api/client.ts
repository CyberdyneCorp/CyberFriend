/**
 * The one place this console talks to the admin API.
 *
 * Every request carries the bearer credential and nothing else that could be
 * read as an identity: no operator field, no impersonation header, no name in
 * a query string. `requestInit` is exported so a test can assert that, because
 * "the request cannot name the operator" is a property of what goes on the
 * wire, not of what this file intends.
 *
 * A 401 is treated as one thing, because the API deliberately makes missing,
 * malformed, unknown and revoked indistinguishable. Guessing which one it was
 * and saying so would rebuild, in the client, the oracle the server refuses to
 * be.
 */

import { currentToken, signOut } from "../auth/session";
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
} from "./types";

/** Absolute: the API is at /api on the origin serving this bundle, whatever
 * path the bundle itself is mounted under. */
const API_ROOT = "/api";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** What a refused credential says. One sentence for all four causes. */
export const REFUSED_MESSAGE = "That credential was refused.";

type Method = "GET" | "POST" | "PUT" | "DELETE";

export function requestInit(
  method: Method,
  token: string | null,
  body?: unknown,
): RequestInit {
  const headers: Record<string, string> = { accept: "application/json" };
  if (token !== null) headers.authorization = `Bearer ${token}`;
  if (body !== undefined) headers["content-type"] = "application/json";
  return {
    method,
    headers,
    // Same-origin by construction; sending cookies would give the API a
    // second, ambient way to be authenticated, and only the bearer token
    // names an operator.
    credentials: "omit",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}

/** `/api/tokens/{id}` with the id escaped: names come from the API, not us. */
export function apiPath(...segments: string[]): string {
  return [API_ROOT, ...segments.map(encodeURIComponent)].join("/");
}

async function failureMessage(response: Response): Promise<string> {
  try {
    const parsed: unknown = await response.json();
    if (parsed !== null && typeof parsed === "object") {
      const record = parsed as Record<string, unknown>;
      for (const field of ["error", "detail", "message", "reason"]) {
        const value = record[field];
        if (typeof value === "string" && value.trim() !== "") return value;
      }
    }
  } catch {
    // A refusal with no JSON body is still a refusal.
  }
  return `${response.status} ${response.statusText}`.trim();
}

async function send<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, requestInit(method, currentToken(), body));
  if (response.status === 401) {
    // Drop the credential before anything retries with it.
    signOut();
    throw new ApiError(401, REFUSED_MESSAGE);
  }
  if (!response.ok) throw new ApiError(response.status, await failureMessage(response));
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text === "" ? undefined : (JSON.parse(text) as T)) as T;
}

/**
 * Check a credential without signing in with it.
 *
 * Deliberately takes the token as an argument instead of reading the session.
 * Putting it in the session first and checking afterwards is what this
 * function exists to avoid: the app would render the signed-in shell for as
 * long as the check took, then throw the operator back to a *blank* sign-in
 * form -- the component having been unmounted and remounted -- with the
 * refusal lost. A wrong token would look exactly like a button that does
 * nothing.
 *
 * It does not sign anybody out on a 401 either: nobody is signed in yet, and
 * clearing the session here would be clearing somebody else's.
 */
export async function verifyCredential(token: string): Promise<void> {
  const response = await fetch(apiPath("status"), requestInit("GET", token));
  if (response.status === 401) throw new ApiError(401, REFUSED_MESSAGE);
  if (!response.ok) throw new ApiError(response.status, await failureMessage(response));
}

export const api = {
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

/** The API's own sentence about what a change did, when it wrote one. */
export function detailOf(result: Changed | Partial<Changed>, fallback: string): string {
  return typeof result.detail === "string" && result.detail.trim() !== ""
    ? result.detail
    : fallback;
}
