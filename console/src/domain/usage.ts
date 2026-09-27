/**
 * The usage screen's rules that need no API: the window, and how figures read.
 *
 * The API refuses a window over 90 days; checking it here too only saves a
 * round trip and says why before anything is sent.
 */

import type { UsageWindow } from "./types";

export const MAX_WINDOW_DAYS = 90;
export const DEFAULT_WINDOW_DAYS = 30;
/** The windows offered as one click. */
export const PRESET_DAYS: readonly number[] = [7, 30, 90];

const DAY_MS = 86_400_000;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** `YYYY-MM-DD` of `at` in UTC: the API counts whole UTC days. */
export function isoDay(at: Date): string {
  return at.toISOString().slice(0, 10);
}

/** The last `days` days, today (the UTC day of `now`, in ms) included. */
export function lastDays(days: number, now: number): UsageWindow {
  return { from: isoDay(new Date(now - (days - 1) * DAY_MS)), to: isoDay(new Date(now)) };
}

function dayOf(value: string): number | null {
  if (!ISO_DAY.test(value)) return null;
  const at = Date.parse(`${value}T00:00:00Z`);
  return Number.isNaN(at) || isoDay(new Date(at)) !== value ? null : at;
}

/** Why the API would refuse `window`, or null when it would not. */
export function windowProblem(window: UsageWindow): string | null {
  const from = dayOf(window.from);
  const to = dayOf(window.to);
  if (from === null || to === null) return "Pick both dates.";
  if (from > to) return "The start must not be after the end.";
  if ((to - from) / DAY_MS + 1 > MAX_WINDOW_DAYS) return `The window is at most ${MAX_WINDOW_DAYS} days.`;
  return null;
}

/** A figure that does not apply (null) reads as a dash, never as zero. */
export function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString();
}

/** USD with enough places that a cheap model's cost is not rounded to $0.00. */
export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const places = value !== 0 && Math.abs(value) < 1 ? 4 : 2;
  return `$${value.toFixed(places)}`;
}

/** Voice time as minutes, or seconds under one. */
export function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 60) return `${value} s`;
  return `${Math.round(value / 60).toLocaleString()} min`;
}
