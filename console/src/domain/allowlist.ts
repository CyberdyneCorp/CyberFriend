/**
 * Whether a discovered tool is already on the allowlist.
 *
 * The server list may say so itself (`allowlisted`); an older API does not,
 * and then the allowlist is the answer. Both are keyed `server/tool`.
 */

import type { AllowlistEntry, FederatedTool } from "./types";

/** `server/tool`, the key an allowlist entry and a discovered tool share. */
export function toolKey(server: string, tool: string): string {
  return `${server}/${tool}`;
}

/** The allowlist's keys. Rebuilt whole from each response, never edited. */
export function allowlistKeys(entries: readonly AllowlistEntry[] | null): ReadonlySet<string> {
  return new Set((entries ?? []).map((entry) => toolKey(entry.server, entry.tool)));
}

/** The server's own flag when it sent one, the allowlist otherwise. */
export function isAllowlisted(
  server: string,
  tool: Pick<FederatedTool, "name" | "allowlisted">,
  allowed: ReadonlySet<string>,
): boolean {
  return tool.allowlisted ?? allowed.has(toolKey(server, tool.name));
}
