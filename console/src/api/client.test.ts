import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import { signIn, signOut } from "../auth/session";
import { api, apiPath, requestInit } from "./client";

afterEach(() => signOut());

describe("requestInit", () => {
  it("carries the credential and nothing that could name an operator", () => {
    const init = requestInit("POST", "cfa_secret", { server: "s", tool: "t" });
    const headers = init.headers as Record<string, string>;
    expect(headers.authorization).toBe("Bearer cfa_secret");
    // The operator comes from the credential alone. Any header or body field
    // that could assert one would make the audit trail a claim.
    expect(Object.keys(headers).sort()).toEqual(["accept", "authorization", "content-type"]);
    expect(String(init.body)).not.toContain("operator");
    expect(init.credentials).toBe("omit");
  });

  it("sends no Authorization header when nobody is signed in", () => {
    const headers = requestInit("GET", null).headers as Record<string, string>;
    expect("authorization" in headers).toBe(false);
  });
});

describe("apiPath", () => {
  it("escapes segments that come from the API rather than from us", () => {
    expect(apiPath("federation", "allowlist", "srv a", "tool/../x")).toBe(
      "/api/federation/allowlist/srv%20a/tool%2F..%2Fx",
    );
  });

  it("targets /api on the origin serving the bundle", () => {
    expect(apiPath("status")).toBe("/api/status");
  });
});

describe("session", () => {
  it("holds the token only in memory", () => {
    signIn("cfa_x");
    const headers = requestInit("GET", "cfa_x").headers as Record<string, string>;
    expect(headers.authorization).toBe("Bearer cfa_x");
    signOut();
    expect(requestInit("GET", null).headers).not.toHaveProperty("authorization");
  });
});

describe("what this client is not able to ask for", () => {
  it("has no call that mints an MCP credential", () => {
    // A minted MCP credential reads one Discord account's whole view of the
    // corpus. The console configures the agent and has no route to its
    // content, so the call that would create that route does not exist here
    // and the API has no route behind it either.
    expect(Object.keys(api).filter((name) => /issue|mint|rotate/i.test(name))).toEqual([]);
  });

  it("never sends a write to the tokens collection", () => {
    // DELETE /api/tokens/{id} revokes and can only narrow. Anything that
    // creates one is what this asserts is absent, in the source rather than
    // in the exported shape, so a hand-rolled fetch fails it too.
    const source = readFileSync(fileURLToPath(new URL("./client.ts", import.meta.url)), "utf8");
    expect(source).not.toMatch(/"(POST|PUT|PATCH)",\s*apiPath\("tokens"/);
  });
});
