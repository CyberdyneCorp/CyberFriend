/**
 * Signing in and out, and finding out who is signed in.
 *
 * `login` is a top-level navigation, not a request: the admin API runs the
 * CyberdyneAuth flow itself and comes back to the console holding only an
 * httpOnly cookie, so no code or token ever passes through this bundle.
 */

import { authPath, currentSession, logout, signInConfig, verifyCredential } from "./http";

export const sessionApi = {
  /** Who the session cookie names, or null. */
  me: currentSession,
  /** Whether "Sign in with CyberdyneAuth" is offered. */
  config: signInConfig,
  /** Where the sign-in button goes: `auth/login` beside the page. */
  loginUrl: (): string => authPath("login"),
  /** End the cookie session; the provider's end-session URL, if any. */
  logout,
  /** Check a break-glass token and say whom it names, without holding it. */
  verifyToken: verifyCredential,
  /** Send the browser to `url`, leaving the console (the provider's sign-out). */
  leave: (url: string): void => {
    window.location.assign(url);
  },
};

/** What a view-model is handed: the real one, or a fake with the same shape. */
export type SessionApi = typeof sessionApi;
