/**
 * The person's own suggestions, and a form to add one.
 *
 * The same rules as `/suggest` in Discord, applied by the server: no contact
 * details, one row per suggestion, five a day. A refusal comes back with its
 * reason, which is shown as it is.
 */

import type { MySuggestion } from "../../domain/me";
import type { UserApi } from "../../services/userApi";
import { Action } from "../action.svelte";
import { Resource } from "../resource.svelte";

export const MAX_SUGGESTION_CHARS = 1000;

export class MyFeatureRequestsVM {
  readonly list: Resource<MySuggestion[]>;
  readonly submitting = new Action();
  draft = $state("");

  readonly canSubmit = $derived(
    !this.submitting.busy && this.draft.trim() !== "" && this.draft.length <= MAX_SUGGESTION_CHARS,
  );

  readonly #api: Pick<UserApi, "suggestions" | "suggest">;

  constructor(api: Pick<UserApi, "suggestions" | "suggest">) {
    this.#api = api;
    this.list = new Resource(() => api.suggestions());
  }

  submit = async (): Promise<void> => {
    if (!this.canSubmit) return;
    const text = this.draft;
    await this.submitting.run(async () => {
      const result = await this.#api.suggest(text);
      this.draft = "";
      await this.list.reload();
      return result.message;
    });
  };
}
