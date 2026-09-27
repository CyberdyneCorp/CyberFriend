/**
 * Who is signed in, held in module variables and nowhere else.
 *
 * Two ways in, one record. A CyberdyneAuth sign-in leaves the credential in
 * an httpOnly cookie this code cannot read, so all the console holds is the
 * principal the API reported. The break-glass form holds an operator token
 * as well, in memory only.
 *
 * Not `localStorage`, not `sessionStorage`, not IndexedDB. This console can
 * add a federated server and enable a tool that changes state somewhere else,
 * so a token in browser storage would outlive the tab, the session and the
 * attention of the person who pasted it. A reload asks for the token again,
 * and `no-browser-storage.test.ts` fails the build if anybody smooths it over.
 *
 * Only `http.ts` reads the token. Everything else asks who is signed in,
 * which is all a view-model needs to know.
 */

import type { Principal } from "../domain/types";

let principal: Principal | null = null;
let token: string | null = null;

const listeners = new Set<() => void>();

function announce(): void {
  for (const listener of listeners) listener();
}

/** Called whenever somebody signs in or out; returns the unsubscribe. */
export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The break-glass token for the next request, or null in cookie mode. */
export function currentToken(): string | null {
  return token;
}

/** Who the API says is signed in, or null. */
export function currentPrincipal(): Principal | null {
  return principal;
}

/**
 * Hold who is signed in, and the break-glass token when that is how.
 *
 * A blank token is ignored rather than held: it would send every request
 * with an empty credential and read as the API being broken.
 */
export function signIn(who: Principal, breakGlassToken: string | null = null): void {
  const trimmed = breakGlassToken?.trim() ?? null;
  if (trimmed === "") return;
  principal = who;
  token = trimmed;
  announce();
}

/**
 * Forget who is signed in, and any token.
 *
 * Called on an explicit sign-out and on any 401. The second case matters
 * more: a revoked token that stayed in memory would keep every screen
 * retrying with a credential that will never work again, and the operator
 * would read the resulting error as the API being down.
 */
export function signOut(): void {
  principal = null;
  token = null;
  announce();
}

/** The session as a port, so a view-model can be handed a fake one. */
export interface SessionPort {
  principal: () => Principal | null;
  subscribe: (listener: () => void) => () => void;
  signIn: (who: Principal, breakGlassToken?: string | null) => void;
  signOut: () => void;
}

export const session: SessionPort = {
  principal: currentPrincipal,
  subscribe,
  signIn,
  signOut,
};
