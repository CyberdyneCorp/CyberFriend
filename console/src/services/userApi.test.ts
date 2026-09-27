import { afterEach, describe, expect, it, vi } from "vitest";

import { signIn, currentPrincipal, signOut } from "./session";
import { userApi } from "./userApi";

afterEach(() => {
  vi.unstubAllGlobals();
  signOut();
});

describe("the user API", () => {
  it("sends the cookie and the CSRF header, never a console token", async () => {
    signIn({ subject: "ana", display: "ana", roles: ["admin"], via: "token" }, "cfa_secret");
    const fetch = vi.fn(async () => new Response(JSON.stringify({ signed_out: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);

    await userApi.logout();

    const [path, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/me/logout");
    expect(init.credentials).toBe("same-origin");
    const headers = init.headers as Record<string, string>;
    expect(headers["x-cyberfriend-console"]).toBe("1");
    expect(Object.keys(headers).map((h) => h.toLowerCase())).not.toContain("authorization");
  });

  it("reads a 401 as no user session, and leaves the console signed in", async () => {
    signIn({ subject: "ana", display: "ana", roles: ["admin"], via: "oidc" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"error":"unauthorized"}', { status: 401 })));

    expect(await userApi.session()).toBeNull();
    expect(currentPrincipal()).not.toBeNull();
  });
});
