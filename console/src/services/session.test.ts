import { afterEach, describe, expect, it, vi } from "vitest";

import type { Principal } from "../domain/types";
import { send } from "./http";
import { currentPrincipal, signIn, signOut, subscribe } from "./session";

const realFetch = globalThis.fetch;

const SAM: Principal = { subject: "sam", display: "sam", roles: ["operator"], via: "token" };

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
  it("holds a break-glass token without the whitespace it was pasted with", async () => {
    signIn(SAM, "  cfa_x \n");
    expect(await sentAuthorization()).toBe("Bearer cfa_x");
  });

  it("ignores a blank token rather than signing in with it", async () => {
    let announced = 0;
    const unsubscribe = subscribe(() => (announced += 1));
    signIn(SAM, " \n\t");
    unsubscribe();
    expect(currentPrincipal()).toBeNull();
    expect(announced).toBe(0);
    expect(await sentAuthorization()).toBeUndefined();
  });

  it("holds a cookie session's principal with no token at all", async () => {
    signIn({ ...SAM, via: "oidc" });
    expect(currentPrincipal()?.via).toBe("oidc");
    expect(await sentAuthorization()).toBeUndefined();
  });

  it("forgets the principal and the token together", async () => {
    signIn(SAM, "cfa_x");
    signOut();
    expect(currentPrincipal()).toBeNull();
    expect(await sentAuthorization()).toBeUndefined();
  });
});
