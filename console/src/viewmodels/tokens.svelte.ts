/**
 * MCP credentials: review them, and revoke one.
 *
 * There is no issue call, and its absence is the point. An MCP credential is
 * one person's read of the corpus, so minting one from this console would let
 * an operator holding only a `cfa_` token mint a `cfm_` one for any Discord
 * account and read what that account can see. Issuing stays in a shell on the
 * host (`python -m chatmemory.mcp.issue_token`). Revoking stays here, because
 * taking access away is what an operator needs at 3am and it cannot widen
 * anything.
 */

import type { TokenRecord } from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { Action } from "./action.svelte";
import { Resource } from "./resource.svelte";

export const REVOKED = "Revoked. It stops working immediately.";

/** What the row's last cell offers: nothing, a revoke, or why there is none. */
export type TokenState = "revoked" | "revocable" | "no-id";

export function tokenState(token: Pick<TokenRecord, "id" | "revoked_at">): TokenState {
  if (token.revoked_at) return "revoked";
  return token.id ? "revocable" : "no-id";
}

export class TokensVM {
  readonly tokens: Resource<TokenRecord[]>;
  readonly action = new Action();

  readonly #api: Pick<AdminApi, "tokens" | "revokeToken">;

  constructor(api: Pick<AdminApi, "tokens" | "revokeToken">) {
    this.#api = api;
    this.tokens = new Resource(() => api.tokens());
  }

  load = (): Promise<void> => this.tokens.reload();

  revoke = async (token: TokenRecord): Promise<void> => {
    const id = token.id;
    if (!id) return;
    await this.action.run(async () => {
      await this.#api.revokeToken(id);
      void this.load();
      return REVOKED;
    });
  };
}
