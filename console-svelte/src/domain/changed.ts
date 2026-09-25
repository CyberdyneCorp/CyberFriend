import type { Changed } from "./types";

/** The API's own sentence about what a change did, when it wrote one. */
export function detailOf(result: Changed | Partial<Changed>, fallback: string): string {
  return typeof result.detail === "string" && result.detail.trim() !== ""
    ? result.detail
    : fallback;
}
