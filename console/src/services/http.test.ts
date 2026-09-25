import { afterEach, describe, expect, it, vi } from "vitest";

import type { Principal } from "../domain/types";
import {
  ApiError,
  authPath,
  currentSession,
  logout,
  REFUSED_MESSAGE,
  send,
  signInConfig,
  verifyCredential,
} from "./http";
import { currentPrincipal, signIn, signOut } from "./session";

const realFetch = globalThis.fetch;

const ANA: Principal = { subject: "ana-sub", display: "ana@example.com", roles: ["admin", "operator"], via: "oidc" };
const TOKEN_OPERATOR: Principal = { subject: "sam", display: "sam", roles: ["admin", "operator"], via: "token" };

/** Answer every request with `status` and `body`, and remember what was sent. */
function respond(status: number, body: unknown): ReturnType<typeof vi.fn> {
  const seen = vi.fn(async () =>
    new Response(body === undefined ? null : JSON.stringify(body), { status }),
  );
  globalThis.fetch = seen as unknown as typeof fetch;
  return seen;
}

/** The RequestInit of the `index`th request made. */
function sentInit(seen: ReturnType<typeof vi.fn>, index = 0): RequestInit {
  return (seen.mock.calls[index] as unknown[])[1] as RequestInit;
}

function sentHeaders(seen: ReturnType<typeof vi.fn>, index = 0): Record<string, string> {
  return sentInit(seen, index).headers as Record<string, string>;
}

afterEach(() => {
  globalThis.fetch = realFetch;
  signOut();
});

describe("send in cookie mode", () => {
  it("sends no Authorization header and lets the browser attach the cookie", async () => {
    signIn(ANA);
    const seen = respond(200, {});
    await send("GET", "/api/status");
    await send("PUT", "/api/settings/x", { value: "1" });
    for (const index of [0, 1]) {
      expect(sentHeaders(seen, index)).not.toHaveProperty("authorization");
      expect(sentInit(seen, index).credentials).toBe("same-origin");
    }
    expect(sentHeaders(seen, 1)["x-cyberfriend-console"]).toBe("1");
  });

  it("signs out on a 401 and says one fixed sentence", async () => {
    signIn(ANA);
    respond(401, { error: "session expired at 09:00" });
    await expect(send("GET", "/api/status")).rejects.toEqual(new ApiError(401, REFUSED_MESSAGE));
    // The server's own words are not repeated: they could tell expired from unknown.
    expect(currentPrincipal()).toBeNull();
  });

  it("reports the API's own reason for any other refusal", async () => {
    signIn(ANA);
    respond(403, { error: "requires admin" });
    await expect(send("POST", "/api/optouts", {})).rejects.toThrow("requires admin");
    expect(currentPrincipal()).toEqual(ANA);
  });

  it("returns nothing for a 204", async () => {
    respond(204, undefined);
    await expect(send("DELETE", "/api/tokens/t1")).resolves.toBeUndefined();
  });
});

describe("send in token mode", () => {
  it("sends the bearer with credentials omitted, so no cookie rides along", async () => {
    signIn(TOKEN_OPERATOR, "cfa_break_glass");
    const seen = respond(200, {});
    await send("DELETE", "/api/tokens/t1");
    expect(sentHeaders(seen).authorization).toBe("Bearer cfa_break_glass");
    expect(sentInit(seen).credentials).toBe("omit");
  });

  it("goes back to cookie mode after signing out", async () => {
    signIn(TOKEN_OPERATOR, "cfa_break_glass");
    signOut();
    const seen = respond(200, {});
    await send("GET", "/api/status");
    expect(sentHeaders(seen)).not.toHaveProperty("authorization");
    expect(sentInit(seen).credentials).toBe("same-origin");
  });
});

describe("currentSession", () => {
  it("names whoever the cookie belongs to", async () => {
    const seen = respond(200, ANA);
    await expect(currentSession()).resolves.toEqual(ANA);
    expect(String(seen.mock.calls[0]?.[0])).toBe("/api/session");
    expect(sentHeaders(seen)).not.toHaveProperty("authorization");
    expect(sentInit(seen).credentials).toBe("same-origin");
  });

  it("is null without a session, and signs nobody out", async () => {
    signIn(TOKEN_OPERATOR, "cfa_current");
    respond(401, {});
    await expect(currentSession()).resolves.toBeNull();
    expect(currentPrincipal()).toEqual(TOKEN_OPERATOR);
  });

  it("passes on a refusal that is not about the credential", async () => {
    respond(403, { error: "no console access" });
    await expect(currentSession()).rejects.toThrow("no console access");
  });
});

describe("verifyCredential", () => {
  it("checks the token as a bearer, without cookies, and says whom it names", async () => {
    const seen = respond(200, TOKEN_OPERATOR);
    await expect(verifyCredential("cfa_candidate")).resolves.toEqual(TOKEN_OPERATOR);
    expect(sentHeaders(seen).authorization).toBe("Bearer cfa_candidate");
    expect(sentInit(seen).credentials).toBe("omit");
  });

  it("does not sign anybody out when the candidate is refused", async () => {
    signIn(ANA);
    respond(401, {});
    await expect(verifyCredential("cfa_wrong")).rejects.toThrow(REFUSED_MESSAGE);
    expect(currentPrincipal()).toEqual(ANA);
  });

  it.each([500, 403])("rejects a %i with the API's reason instead of accepting it", async (status) => {
    respond(status, { detail: "admin database unreachable" });
    await expect(verifyCredential("cfa_candidate")).rejects.toEqual(
      new ApiError(status, "admin database unreachable"),
    );
    expect(currentPrincipal()).toBeNull();
  });
});

describe("signInConfig", () => {
  it("reads whether sign-in is offered", async () => {
    const seen = respond(200, { sign_in: true });
    await expect(signInConfig()).resolves.toEqual({ sign_in: true });
    expect(String(seen.mock.calls[0]?.[0])).toBe("/auth/config");
  });

  it("takes anything but a clear yes as no", async () => {
    respond(404, { error: "no such route" });
    await expect(signInConfig()).resolves.toEqual({ sign_in: false });
    respond(200, { sign_in: "yes" });
    await expect(signInConfig()).resolves.toEqual({ sign_in: false });
    globalThis.fetch = vi.fn(async () => {
      throw new TypeError("network down");
    }) as unknown as typeof fetch;
    await expect(signInConfig()).resolves.toEqual({ sign_in: false });
  });
});

describe("logout", () => {
  it("posts with the console header and the cookie, and returns the end-session URL", async () => {
    const seen = respond(200, { end_session_url: "https://auth.example/logout?id_token_hint=x" });
    await expect(logout()).resolves.toEqual({
      end_session_url: "https://auth.example/logout?id_token_hint=x",
    });
    expect(String(seen.mock.calls[0]?.[0])).toBe("/auth/logout");
    expect(sentInit(seen).method).toBe("POST");
    expect(sentInit(seen).credentials).toBe("same-origin");
    expect(sentHeaders(seen)["x-cyberfriend-console"]).toBe("1");
    expect(sentHeaders(seen)).not.toHaveProperty("authorization");
  });

  it("returns no URL when the server has none", async () => {
    respond(200, { end_session_url: null });
    await expect(logout()).resolves.toEqual({ end_session_url: null });
  });

  it("throws when the server refused", async () => {
    respond(403, { error: "cross-site request refused" });
    await expect(logout()).rejects.toThrow("cross-site request refused");
  });
});

describe("authPath", () => {
  it("resolves beside the page, prefix included", () => {
    expect(authPath("login", "https://ops.example/")).toBe("/auth/login");
    expect(authPath("login", "https://ops.example/cyberfriend/index.html#/status")).toBe(
      "/cyberfriend/auth/login",
    );
  });
});
