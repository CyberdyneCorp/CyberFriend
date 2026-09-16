/** Rendering helpers. Pure, so the status screen's tolerance is testable. */

import type { JsonValue } from "../api/types";

const ISO_LIKE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;

/** A timestamp in the operator's own zone, or the raw string if it is not one. */
export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  if (!ISO_LIKE.test(value)) return value;
  const at = new Date(value);
  return Number.isNaN(at.getTime()) ? value : at.toLocaleString();
}

/** Largest first, so the first one that fits is the one to say. */
const SCALES: readonly [number, string][] = [
  [86400, "day"],
  [3600, "hour"],
  [60, "minute"],
];

export function formatRelative(value: string | null | undefined, now = Date.now()): string {
  if (!value || !ISO_LIKE.test(value)) return "—";
  const at = new Date(value).getTime();
  if (Number.isNaN(at)) return "—";
  const seconds = Math.round((now - at) / 1000);
  if (seconds < 60) return "just now";
  for (const [size, unit] of SCALES) {
    if (seconds < size) continue;
    const count = Math.floor(seconds / size);
    return `${count} ${unit}${count === 1 ? "" : "s"} ago`;
  }
  return "just now";
}

/** A scalar as an operator should read it. Objects and arrays are JSON. */
export function formatValue(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return value.toLocaleString();
  if (typeof value === "string") return ISO_LIKE.test(value) ? formatTime(value) : value;
  if (Array.isArray(value)) return value.length === 0 ? "—" : value.map(formatValue).join(", ");
  return JSON.stringify(value);
}

/** `indexed_channel_ids` -> `Indexed channel ids`. */
export function humanise(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * Split an arbitrary status group into the scalars a card can show and the
 * nested groups that get cards of their own. The status endpoint's inner
 * field names are the API's business; this console renders whatever arrives
 * rather than showing blanks where a guessed name did not match.
 */
export function partition(group: JsonValue): {
  scalars: [string, JsonValue][];
  groups: [string, JsonValue][];
} {
  if (group === null || typeof group !== "object" || Array.isArray(group)) {
    return { scalars: [], groups: [] };
  }
  const entries = Object.entries(group) as [string, JsonValue][];
  return {
    scalars: entries.filter(([, v]) => v === null || typeof v !== "object" || Array.isArray(v)),
    groups: entries.filter(([, v]) => v !== null && typeof v === "object" && !Array.isArray(v)),
  };
}
