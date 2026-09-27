/**
 * Feature requests: the statuses, and what an admin's edit sends.
 *
 * Only the fields that changed are sent, so the change record holds one
 * entry per thing the admin actually changed and an untouched note is never
 * rewritten.
 */

import type { FeatureRequest, FeatureRequestPatch, FeatureRequestStatus } from "./types";

export const STATUSES: readonly FeatureRequestStatus[] = [
  "new",
  "triaged",
  "planned",
  "done",
  "declined",
  "duplicate",
];

/** The edit form's values, as typed. */
export interface Draft {
  id: number;
  status: FeatureRequestStatus;
  note: string;
  /** The original's number, or "" for none. */
  duplicateOf: string;
}

export function draftOf(row: FeatureRequest): Draft {
  return {
    id: row.id,
    status: row.status,
    note: row.admin_note ?? "",
    duplicateOf: row.duplicate_of === null ? "" : String(row.duplicate_of),
  };
}

/** Why the draft cannot be sent, or null. */
export function draftProblem(draft: Draft): string | null {
  const duplicate = draft.duplicateOf.trim();
  if (duplicate !== "" && !/^\d+$/.test(duplicate)) return "Duplicate of must be a suggestion number.";
  if (duplicate === String(draft.id)) return "A suggestion cannot be a duplicate of itself.";
  return null;
}

/** The fields that differ from the row, as the API takes them. */
export function patchFor(row: FeatureRequest, draft: Draft): FeatureRequestPatch {
  const patch: FeatureRequestPatch = {};
  if (draft.status !== row.status) patch.status = draft.status;
  const note = draft.note.trim() === "" ? null : draft.note.trim();
  if (note !== row.admin_note) patch.admin_note = note;
  const duplicate = draft.duplicateOf.trim() === "" ? null : Number(draft.duplicateOf.trim());
  if (duplicate !== row.duplicate_of) patch.duplicate_of = duplicate;
  return patch;
}
