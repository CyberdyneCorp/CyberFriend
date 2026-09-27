import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { adminApi } from "./adminApi";
import { apiPath, apiRoot, requestInit } from "./http";
describe("requestInit", () => {
  it("carries the cookie and nothing that could name an operator", () => {
    const init = requestInit("POST", { server: "s", tool: "t" });
    const headers = init.headers as Record<string, string>;
    // The operator comes from the credential alone. Any header or body field
    // that could assert one would make the audit trail a claim.
    expect(Object.keys(headers).sort()).toEqual(["accept", "content-type", "x-cyberfriend-console"]);
    expect(String(init.body)).not.toContain("operator");
    expect(init.credentials).toBe("same-origin");
  });

  it("sends the console header on writes only", () => {
    expect(requestInit("GET").headers).not.toHaveProperty("x-cyberfriend-console");
    for (const method of ["POST", "PUT", "DELETE"] as const) {
      expect(requestInit(method).headers).toHaveProperty("x-cyberfriend-console", "1");
    }
  });
});

describe("apiPath", () => {
  it("escapes segments that come from the API rather than from us", () => {
    expect(apiPath("federation", "allowlist", "srv a", "tool/../x")).toBe(
      "/api/federation/allowlist/srv%20a/tool%2F..%2Fx",
    );
  });

  it("targets /api beside a console served at the root", () => {
    expect(apiPath("status")).toBe("/api/status");
    expect(apiRoot("https://ops.example/")).toBe("/api");
    expect(apiRoot("https://ops.example/index.html#/status")).toBe("/api");
  });

  it("targets /api under the prefix the console is mounted behind", () => {
    expect(apiRoot("https://ops.example/cyberfriend/")).toBe("/cyberfriend/api");
    expect(apiRoot("https://ops.example/cyberfriend/index.html#/tokens")).toBe("/cyberfriend/api");
  });
});

describe("what this client is not able to ask for", () => {
  it("has no call that mints an MCP credential", () => {
    // A minted MCP credential reads one Discord account's whole view of the
    // corpus. The console configures the agent and has no route to its
    // content, so the call that would create that route does not exist here
    // and the API has no route behind it either.
    expect(Object.keys(adminApi).filter((name) => /issue|mint|rotate/i.test(name))).toEqual([]);
  });

  it("never sends a write to the tokens collection", () => {
    // DELETE /api/tokens/{id} revokes and can only narrow. Anything that
    // creates one is what this asserts is absent, in the source rather than
    // in the exported shape, so a hand-rolled call fails it too.
    const source = readFileSync(fileURLToPath(new URL("./adminApi.ts", import.meta.url)), "utf8");
    expect(source).not.toMatch(/"(POST|PUT|PATCH)",\s*apiPath\("tokens"/);
  });
});
