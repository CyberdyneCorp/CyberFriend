/**
 * The break-glass request: an operator token typed into the console.
 *
 * The only file allowed to put a credential in a request header (the storage
 * guard enforces it). Everyday sign-in is the CyberdyneAuth cookie, which
 * JavaScript never sees; this is for when sign-in is not configured, or has
 * been switched off because the identity provider is down.
 *
 * `credentials: "omit"`: a token request carries no cookie, so a browser that
 * still holds a session from an earlier sign-in cannot mix the two. The
 * server ignores cookies on such a request anyway; this makes sure it never
 * has to.
 *
 * It builds the request and does not send it: `http.ts` is the one module
 * that talks to the network.
 */

import type { Method } from "./http";

export function breakGlassInit(method: Method, token: string, body?: unknown): RequestInit {
  const headers: Record<string, string> = {
    accept: "application/json",
    authorization: `Bearer ${token}`,
  };
  if (body !== undefined) headers["content-type"] = "application/json";
  return {
    method,
    headers,
    credentials: "omit",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}
