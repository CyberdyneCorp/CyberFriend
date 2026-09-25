/**
 * Who changed what, when, and from what to what.
 *
 * Configuration here changes without a pull request, so this record is the
 * only remaining review -- which is why it carries the before and the after
 * rather than the fact of a change. Escalations are filterable rather than
 * findable by reading: "what got more permissive, and who did it" is the
 * question this screen exists for.
 */

import type { AuditEntry, ChangeKind } from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { Resource } from "./resource.svelte";

export type AuditFilter = ChangeKind | "all";

export const FILTERS: readonly AuditFilter[] = ["all", "escalation", "refused", "applied"];

export class AuditVM {
  readonly audit: Resource<AuditEntry[]>;
  filter: AuditFilter = $state("all");
  readonly shown: AuditEntry[];

  constructor(api: Pick<AdminApi, "audit">) {
    this.audit = new Resource(() => api.audit());
    this.shown = $derived(
      (this.audit.data ?? []).filter((entry) => this.filter === "all" || entry.kind === this.filter),
    );
  }

  load = (): Promise<void> => this.audit.reload();

  show = (filter: AuditFilter): void => {
    this.filter = filter;
  };
}
