import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, REFUSED_MESSAGE, send, verifyCredential } from "./http";
import { isSignedIn, signIn, signOut } from "./session";

const realFetch = globalThis.fetch;

function respond(status: number, body: unknown): void {
  globalThis.fetch = vi.fn(async () =>
    new Response(body === undefined ? null : JSON.stringify(body), { status }),
  ) as typeof fetch;
}

afterEach(() => {
  globalThis.fetch = realFetch;
  signOut();
});

describe("send", () => {
  it("signs out on a 401 and says one fixed sentence", async () => {
    signIn("cfa_revoked");
    respond(401, { error: "token revoked at 09:00" });
    await expect(send("GET", "/api/status")).rejects.toEqual(new ApiError(401, REFUSED_MESSAGE));
    // The server's own words are not repeated: they could tell revoked from unknown.
    expect(isSignedIn()).toBe(false);
  });

  it("reports the API's own reason for any other refusal", async () => {
    signIn("cfa_ok");
    respond(400, { detail: "window_max_tokens must be a number" });
    await expect(send("PUT", "/api/settings/x", { value: "a" })).rejects.toThrow(
      "window_max_tokens must be a number",
    );
    expect(isSignedIn()).toBe(true);
  });

  it("returns nothing for a 204", async () => {
    respond(204, undefined);
    await expect(send("DELETE", "/api/tokens/t1")).resolves.toBeUndefined();
  });
});

describe("verifyCredential", () => {
  it("does not sign anybody out when the candidate is refused", async () => {
    signIn("cfa_current");
    respond(401, {});
    await expect(verifyCredential("cfa_wrong")).rejects.toThrow(REFUSED_MESSAGE);
    expect(isSignedIn()).toBe(true);
  });
});
