/**
 * What a federated tool does to the world, and when typing its name is required.
 *
 * The API decides this -- `ToolEffect.mutates`, where anything that is not
 * `read_only` counts as mutating -- and sends its verdict as `mutates`. This
 * module prefers that verdict and falls back to reading the effect word only
 * when it is absent.
 *
 * The fallback is the part worth reading: an absent or unrecognised effect is
 * **state-changing**. `undetermined` is what the server sends when it could
 * not tell, an omitted field is what an older or hostile server sends, and a
 * console that rendered either as harmless would be the screen an operator is
 * looking at while deciding. Failing closed here also keeps this screen and
 * the API's refusal saying the same thing, so the operator learns the rule
 * from the interface rather than from an error.
 */

import type { FederatedTool } from "../api/types";

export type Effect = "read-only" | "state-changing";

/**
 * The only spellings that buy a tool the benign badge. `read_only` is the
 * API's own word; the rest are there so a differently-spelled server does not
 * get a *more* permissive reading than it deserves -- matching loosely in the
 * safe direction would mean a new effect word ships as harmless.
 */
const READ_ONLY_SPELLINGS: ReadonlySet<string> = new Set([
  "read_only",
  "read-only",
  "readonly",
  "read",
  "query",
  "safe",
]);

export function effectOf(declared: string | null | undefined): Effect {
  const normalised = (declared ?? "").trim().toLowerCase();
  return READ_ONLY_SPELLINGS.has(normalised) ? "read-only" : "state-changing";
}

export function isStateChanging(declared: string | null | undefined): boolean {
  return effectOf(declared) === "state-changing";
}

/**
 * The verdict for a tool or allowlist entry: the API's `mutates` when it sent
 * one, the effect word otherwise.
 */
export function toolEffect(
  tool: Pick<FederatedTool, "effect" | "mutates">,
): Effect {
  if (typeof tool.mutates === "boolean") {
    return tool.mutates ? "state-changing" : "read-only";
  }
  return effectOf(tool.effect);
}

export function toolMutates(tool: Pick<FederatedTool, "effect" | "mutates">): boolean {
  return toolEffect(tool) === "state-changing";
}

/** How the effect reads to an operator, including the two undeclared cases. */
export function effectLabel(declared: string | null | undefined): string {
  const normalised = (declared ?? "").trim().toLowerCase();
  if (effectOf(normalised) === "read-only") return "read-only";
  // `undetermined` is the API saying it could not tell; an empty field is a
  // server that did not say. Both are treated the same and both are named,
  // because "we could not tell" and "it said so" are different things to know.
  if (normalised === "" ) return "state-changing (undeclared)";
  if (normalised === "undetermined") return "state-changing (undetermined)";
  return "state-changing";
}

/**
 * Whether a typed confirmation matches the tool it claims to enable.
 *
 * Exact after trimming: no case folding, no prefix match. The point of the
 * gate is that the operator read the name of the specific tool they are
 * granting, and a fuzzy match is a gate you can pass while looking at a
 * different row. Leading and trailing whitespace is forgiven because it is an
 * artefact of pasting, not of misreading.
 */
export function confirmationMatches(typed: string, toolName: string): boolean {
  const name = toolName.trim();
  return name !== "" && typed.trim() === name;
}
