/**
 * The one place this console calls `fetch`.
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

export async function send<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, requestInit(method, currentToken(), body));
  if (response.status === 401) {
    // Drop the credential before anything retries with it.
    signOut();
    throw new ApiError(401, REFUSED_MESSAGE);
  }
  if (!response.ok) throw new ApiError(response.status, await failureMessage(response));
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text === "" ? undefined : JSON.parse(text)) as T;
}

/**
 * Check a credential without signing in with it.
 *
 * Deliberately takes the token as an argument instead of reading the session.
 * Putting it in the session first and checking afterwards would render the
 * signed-in shell for as long as the check took, then throw the operator back
 * to a blank sign-in form with the refusal lost.
 *
 * It does not sign anybody out on a 401 either: nobody is signed in yet, and
 * clearing the session here would be clearing somebody else's.
 */
export async function verifyCredential(token: string): Promise<void> {
  const response = await fetch(apiPath("status"), requestInit("GET", token));
  if (response.status === 401) throw new ApiError(401, REFUSED_MESSAGE);
  if (!response.ok) throw new ApiError(response.status, await failureMessage(response));
}
