import { describe, expect, it } from "vitest";

import type { TokenRecord } from "../domain/types";
import { REVOKED, TokensVM, tokenState } from "./tokens.svelte";

const LIVE: TokenRecord = { id: "t1", label: "laptop", person: "discord:42", issued_at: "2026-09-01" };

describe("tokenState", () => {
  it("tells a revoked row, a revocable one and one without an id apart", () => {
    expect(tokenState({ ...LIVE, revoked_at: "2026-09-02" })).toBe("revoked");
    expect(tokenState(LIVE)).toBe("revocable");
    expect(tokenState({ ...LIVE, id: null })).toBe("no-id");
  });
});

describe("TokensVM", () => {
  it("revokes by id", async () => {
    const revoked: string[] = [];
    const vm = new TokensVM({
      tokens: async () => [LIVE],
      revokeToken: async (id) => {
        revoked.push(id);
        return { changed: "mcp_token" };
      },
    });
    await vm.revoke(LIVE);
    expect(revoked).toEqual(["t1"]);
    expect(vm.action.note).toBe(REVOKED);
  });

  it("sends nothing for a row without an id", async () => {
    const revoked: string[] = [];
    const vm = new TokensVM({
      tokens: async () => [],
      revokeToken: async (id) => {
        revoked.push(id);
        return { changed: "mcp_token" };
      },
    });
    await vm.revoke({ ...LIVE, id: null });
    expect(revoked).toEqual([]);
  });

  it("has no way to issue a token", () => {
    const vm = new TokensVM({ tokens: async () => [], revokeToken: async () => ({ changed: "" }) });
    const names = [
      ...Object.keys(vm),
      ...Object.getOwnPropertyNames(Object.getPrototypeOf(vm) as object),
    ];
    expect(names.filter((name) => /issue|mint|create/i.test(name))).toEqual([]);
  });
});
