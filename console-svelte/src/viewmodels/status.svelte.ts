/**
 * The Status screen: health, ingestion, backlog, and what changed recently.
 *
 * Rendered from whatever the status endpoint reports rather than from a fixed
 * list of field names. The contract fixes the categories, not the fields
 * inside them, and the failure of hard-coding guessed names is silent: a card
 * of dashes where the number an operator came here for should be.
 *
 * Counts and timings only. Nothing here is corpus content.
 */

import { partition } from "../domain/format";
import type { AuditEntry, JsonValue, Status } from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { Resource } from "./resource.svelte";

export type Metric = [key: string, value: JsonValue];

export interface StatusGroup {
  name: string;
  metrics: Metric[];
  subgroups: { name: string; metrics: Metric[] }[];
}

/** How many changes the screen shows; the Audit screen has the rest. */
export const RECENT_CHANGES = 8;

function groupOf(name: string, value: JsonValue): StatusGroup {
  const { scalars, groups } = partition(value);
  return {
    name,
    metrics: scalars,
    subgroups: groups.map(([key, nested]) => ({ name: key, metrics: partition(nested).scalars })),
  };
}

export class StatusVM {
  readonly report: Resource<Status>;
  readonly audit: Resource<AuditEntry[]>;

  /** The top-level scalars: the health card. */
  readonly health: Metric[];
  /** Every nested group gets a card of its own. */
  readonly groups: StatusGroup[];
  readonly recent: AuditEntry[];

  constructor(api: Pick<AdminApi, "status" | "audit">) {
    this.report = new Resource(() => api.status());
    this.audit = new Resource(() => api.audit());
    this.health = $derived(this.report.data === null ? [] : partition(this.report.data).scalars);
    this.groups = $derived(
      this.report.data === null
        ? []
        : partition(this.report.data).groups.map(([name, value]) => groupOf(name, value)),
    );
    this.recent = $derived((this.audit.data ?? []).slice(0, RECENT_CHANGES));
  }

  load = async (): Promise<void> => {
    await Promise.all([this.report.reload(), this.audit.reload()]);
  };
}
