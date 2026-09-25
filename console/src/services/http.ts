/**
 * The one place this console calls `fetch`.
 *
 * Cookie mode is the default: requests go out with `credentials:
 * "same-origin"`, so the browser attaches the CyberdyneAuth session cookie
 * that JavaScript cannot read, and every write carries the console's CSRF
 * header. Nothing here ever sets a credential header; a break-glass token is
 * put on the request by `breakGlassHttp.ts`, and only while one is held.
 *
 * Nothing that could be read as an identity goes on the wire besides the
 * credential: no operator field, no impersonation header, no name in a query
 * string. `requestInit` is exported so a test can assert that, because "the
 * request cannot name the operator" is a property of what is sent, not of what
 * this file intends.
 *
 * A 401 is treated as one thing, because the API deliberately makes missing,
 * malformed, unknown, expired and revoked indistinguishable. Guessing which
 * one it was and saying so would rebuild, in the client, the oracle the server
 * refuses to be.
 */

import type { Principal, SignedOut, SignInConfig } from "../domain/types";
import { breakGlassInit } from "./breakGlassHttp";
import { currentToken, signOut } from "./session";

/**
 * Where the API is: `api` beside the page that loaded this bundle.
 *
 * The admin API serves the console at its own root and the API at `/api`
 * under the same root, so mounted under a prefix (`/ops/`) the API is at
 * `/ops/api`, not the origin's `/api`. Resolved against the document on each
 * call rather than once, so it follows the page it runs in; with no document
 * (a node test) the page is taken to be at `/`.
 */
export function apiRoot(baseURI: string | undefined = globalThis.document?.baseURI): string {
  return new URL("api", new URL(".", baseURI ?? "http://localhost/")).pathname;
}

/** `auth/<name>` beside the page, for the same reason as `apiRoot`. */
export function authPath(name: string, baseURI: string | undefined = globalThis.document?.baseURI): string {
  return new URL(`auth/${name}`, new URL(".", baseURI ?? "http://localhost/")).pathname;
}

/** The header every cookie-mode write carries; a cross-site form cannot set it. */
export const CSRF_HEADER = "x-cyberfriend-console";

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

export type Method = "GET" | "POST" | "PUT" | "DELETE";

/** A cookie-mode request: the browser's session cookie, and no credential header. */
export function requestInit(method: Method, body?: unknown): RequestInit {
  const headers: Record<string, string> = { accept: "application/json" };
  if (method !== "GET") headers[CSRF_HEADER] = "1";
  if (body !== undefined) headers["content-type"] = "application/json";
  return {
    method,
    headers,
    credentials: "same-origin",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}

/** The request for whoever is signed in: their break-glass token if they hold one, else the cookie. */
function initFor(method: Method, body?: unknown): RequestInit {
  const token = currentToken();
  return token === null ? requestInit(method, body) : breakGlassInit(method, token, body);
}

/** `/api/tokens/{id}` with each segment escaped: names come from the API, not us. */
export function apiPath(...segments: string[]): string {
  return [apiRoot(), ...segments.map(encodeURIComponent)].join("/");
}

function firstMessage(parsed: unknown): string | null {
  if (parsed === null || typeof parsed !== "object") return null;
  const record = parsed as Record<string, unknown>;
  for (const field of ["error", "detail", "message", "reason"]) {
    const value = record[field];
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return null;
}

async function failureMessage(response: Response): Promise<string> {
  try {
    const message = firstMessage(await response.json());
    if (message !== null) return message;
  } catch {
    // A refusal with no JSON body is still a refusal.
  }
  return `${response.status} ${response.statusText}`.trim();
}

async function parsed<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text === "" ? undefined : JSON.parse(text)) as T;
}

export async function send<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, initFor(method, body));
  if (response.status === 401) {
    // Drop the credential before anything retries with it.
    signOut();
    throw new ApiError(401, REFUSED_MESSAGE);
  }
  if (!response.ok) throw new ApiError(response.status, await failureMessage(response));
  return parsed<T>(response);
}

/**
 * Who the session cookie names, or null when there is no session.
 *
 * Asked once when the console loads: a person who signed in with
 * CyberdyneAuth comes back from the callback holding only a cookie, and this
 * is how the console finds out who that is.
 */
export async function currentSession(): Promise<Principal | null> {
  const response = await fetch(apiPath("session"), requestInit("GET"));
  if (response.status === 401) return null;
  if (!response.ok) throw new ApiError(response.status, await failureMessage(response));
  return parsed<Principal>(response);
}

/**
 * Check a break-glass token without signing in with it, and say whom it names.
 *
 * Deliberately takes the token as an argument instead of reading the session.
 * Putting it in the session first and checking afterwards would render the
 * signed-in shell for as long as the check took, then throw the operator back
 * to a blank sign-in form with the refusal lost.
 *
 * It does not sign anybody out on a 401 either: nobody is signed in yet, and
 * clearing the session here would be clearing somebody else's.
 */
export async function verifyCredential(token: string): Promise<Principal> {
  const response = await fetch(apiPath("session"), breakGlassInit("GET", token));
  if (response.status === 401) throw new ApiError(401, REFUSED_MESSAGE);
  if (!response.ok) throw new ApiError(response.status, await failureMessage(response));
  return parsed<Principal>(response);
}

/**
 * Whether the API offers CyberdyneAuth sign-in.
 *
 * Anything but a clear yes is a no: the break-glass form then stands alone,
 * which is what a deploy without sign-in has always shown.
 */
export async function signInConfig(): Promise<SignInConfig> {
  try {
    const response = await fetch(authPath("config"), requestInit("GET"));
    if (!response.ok) return { sign_in: false };
    const body = await parsed<Partial<SignInConfig> | undefined>(response);
    return { sign_in: body?.sign_in === true };
  } catch {
    return { sign_in: false };
  }
}

/**
 * End the CyberdyneAuth session, and say where the provider's own sign-out is.
 *
 * The server revokes the session before it answers, so the cookie is dead
 * whatever the browser does next.
 */
export async function logout(): Promise<SignedOut> {
  const response = await fetch(authPath("logout"), requestInit("POST"));
  if (!response.ok) throw new ApiError(response.status, await failureMessage(response));
  const body = await parsed<Partial<SignedOut> | undefined>(response);
  const url = body?.end_session_url;
  return { end_session_url: typeof url === "string" && url !== "" ? url : null };
}
