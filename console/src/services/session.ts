/**
 * The admin token, held in a module variable and nowhere else.
 *
 * Not `localStorage`, not `sessionStorage`, not a cookie, not IndexedDB. This
 * console can add a federated server and enable a tool that changes state
 * somewhere else, so its credential is a grant of new capability to the agent
 * -- and a token in browser storage outlives the tab, the session and the
 * attention of the person who pasted it.
 *
 * The cost is real and is the point: a reload asks for the token again, and
 * `no-browser-storage.test.ts` fails the build if anybody smooths it over.
 *
 * Only `http.ts` reads the token. Everything else asks whether somebody is
 * signed in, which is all a view-model needs to know.
 */

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

/** The credential for the next request, or null when nobody is signed in. */
export function currentToken(): string | null {
  return token;
}

export function isSignedIn(): boolean {
  return token !== null;
}

export function signIn(value: string): void {
  const trimmed = value.trim();
  if (trimmed === "") return;
  token = trimmed;
  announce();
}

/**
 * Forget the credential.
 *
 * Called on an explicit sign-out and on any 401. The second case matters more:
 * a revoked token that stayed in memory would keep every screen retrying with
 * a credential that will never work again, and the operator would read the
 * resulting error as the API being down.
 */
export function signOut(): void {
  token = null;
  announce();
}

/** The session as a port, so a view-model can be handed a fake one. */
export interface SessionPort {
  isSignedIn: () => boolean;
  subscribe: (listener: () => void) => () => void;
  signIn: (token: string) => void;
  signOut: () => void;
}

export const session: SessionPort = { isSignedIn, subscribe, signIn, signOut };
