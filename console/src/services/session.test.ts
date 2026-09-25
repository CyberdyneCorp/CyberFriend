import { afterEach, describe, expect, it, vi } from "vitest";

import { send } from "./http";
import { isSignedIn, signIn, signOut, subscribe } from "./session";

const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
  signOut();
});

/**
 * The Authorization header the next request carries. Read off the wire
 * rather than with `currentToken`: the storage guard allows one reader of the
 * credential, and a test is not it.
 */
async function sentAuthorization(): Promise<string | undefined> {
  const seen = vi.fn(async () => new Response(null, { status: 204 }));
  globalThis.fetch = seen as unknown as typeof fetch;
  await send("GET", "/api/status");
  const init = (seen.mock.calls[0] as unknown[] | undefined)?.[1] as RequestInit | undefined;
  return (init?.headers as Record<string, string> | undefined)?.authorization;
}

describe("session", () => {
  it("holds the credential without the whitespace it was pasted with", async () => {
    signIn("  cfa_x \n");
    expect(await sentAuthorization()).toBe("Bearer cfa_x");
  });

  it("ignores a blank credential rather than signing in with it", async () => {
    let announced = 0;
    const unsubscribe = subscribe(() => (announced += 1));
    signIn(" \n\t");
    unsubscribe();
    expect(isSignedIn()).toBe(false);
    expect(announced).toBe(0);
    expect(await sentAuthorization()).toBeUndefined();
  });
});
