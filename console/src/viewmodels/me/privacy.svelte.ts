/**
 * The person's own privacy dashboard, and "delete everything".
 *
 * The same inventory `/privacy` shows in a DM, read from `/me/privacy`, which
 * finds the person from the signed-in account's link and nothing this page
 * sends. Deleting needs three things, as the server requires: one of the two
 * choices, the typed word, and a sign-in from the last five minutes. Without
 * the last, the page offers to sign in again rather than sending a request the
 * server will refuse.
 */

import { CONFIRM_WORD, retentionLines, type Erased, type ErasureMode, type MePrivacy } from "../../domain/me";
import type { UserApi } from "../../services/userApi";
import { Action } from "../action.svelte";
import { Resource } from "../resource.svelte";

export class PrivacyVM {
  readonly report: Resource<MePrivacy>;
  readonly erasing = new Action();
  mode: ErasureMode = $state("erase");
  typed = $state("");

  /** What is kept and for how long, as sentences. */
  readonly retention: string[];
  readonly confirmed = $derived(this.typed.trim() === CONFIRM_WORD);

  readonly #api: Pick<UserApi, "privacy" | "erase" | "freshUrl" | "leave">;
  readonly #fresh: () => boolean;
  readonly #erased: (erased: Erased) => void;

  constructor(
    api: Pick<UserApi, "privacy" | "erase" | "freshUrl" | "leave">,
    fresh: () => boolean,
    erased: (erased: Erased) => void,
  ) {
    this.#api = api;
    this.#fresh = fresh;
    this.#erased = erased;
    this.report = new Resource(() => api.privacy());
    this.retention = $derived(
      this.report.data === null ? [] : retentionLines(this.report.data.retention),
    );
  }

  /** Whether the server will want a new sign-in before deleting. */
  get needsFreshSignIn(): boolean {
    return !this.#fresh();
  }

  canErase = (): boolean => this.confirmed && !this.erasing.busy && !this.needsFreshSignIn;

  /** Sign in again, with a maximum age; the API brings the browser back here. */
  signInAgain = (): void => {
    this.#api.leave(this.#api.freshUrl());
  };

  erase = async (): Promise<void> => {
    if (!this.canErase()) return;
    await this.erasing.run(async () => {
      const erased = await this.#api.erase(this.mode, CONFIRM_WORD);
      this.typed = "";
      // Erased, the session is over: the server ended it with the link.
      this.#erased(erased);
    });
  };
}
