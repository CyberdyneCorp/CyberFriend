/**
 * What people suggested, and its triage.
 *
 * Operators read the list; admins set a status, a note and a duplicate link,
 * one row at a time. The server refuses an operator's change whatever this
 * screen shows, and records every admin change with who made it. Authors who
 * asked to hear about a status change are told by the bot, not from here.
 */

import { draftOf, draftProblem, patchFor, type Draft } from "../domain/featureRequests";
import type { FeatureRequest, FeatureRequestPage, FeatureRequestStatus } from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { Action } from "./action.svelte";
import { Resource } from "./resource.svelte";

export type StatusFilter = FeatureRequestStatus | "all";

export const NOTHING_CHANGED = "Nothing changed.";

type Api = Pick<AdminApi, "featureRequests" | "triageFeatureRequest">;

export class FeatureRequestsVM {
  filter: StatusFilter = $state("all");
  page = $state(1);
  readonly requests: Resource<FeatureRequestPage>;
  readonly action = new Action();
  /** The row being edited, or null. */
  draft: Draft | null = $state(null);
  readonly pages: number;

  readonly #api: Api;

  constructor(api: Api) {
    this.#api = api;
    this.requests = new Resource(() =>
      api.featureRequests(this.filter === "all" ? null : this.filter, this.page),
    );
    this.pages = $derived(pageCount(this.requests.data));
  }

  load = (): Promise<void> => this.requests.reload();

  show = (filter: StatusFilter): void => {
    this.filter = filter;
    this.page = 1;
    this.draft = null;
    void this.load();
  };

  turn = (by: number): void => {
    const next = this.page + by;
    if (next < 1 || next > this.pages) return;
    this.page = next;
    this.draft = null;
    void this.load();
  };

  edit = (row: FeatureRequest): void => {
    this.action.clear();
    this.draft = draftOf(row);
  };

  cancel = (): void => {
    this.draft = null;
  };

  save = async (row: FeatureRequest): Promise<void> => {
    const draft = this.draft;
    if (draft === null || draft.id !== row.id) return;
    await this.action.run(async () => {
      const problem = draftProblem(draft);
      if (problem !== null) throw new Error(problem);
      const patch = patchFor(row, draft);
      if (Object.keys(patch).length === 0) {
        this.draft = null;
        return NOTHING_CHANGED;
      }
      // Closed only once the server took it, so a refusal keeps what was typed.
      await this.#api.triageFeatureRequest(row.id, patch);
      if (this.draft === draft) this.draft = null;
      void this.load();
      return `#${row.id} saved.`;
    });
  };
}

function pageCount(page: FeatureRequestPage | null): number {
  if (page === null || page.total === 0) return 1;
  return Math.ceil(page.total / page.page_size);
}
