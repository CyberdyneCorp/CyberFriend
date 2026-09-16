/**
 * The badges, in one file, because the invariants they carry are visual.
 *
 * `ToolEffectBadge` is the important one: a federated tool that changes state
 * must look different from one that does not *everywhere it appears* -- in a
 * server's discovered tool list, in the allowlist, and in the confirmation
 * that enables it. One component is how that stays true when a fourth place
 * to show a tool gets added. Colour alone is not the signal; the word is
 * there, and so is a glyph, because a red badge and an amber one are the same
 * badge to a good share of operators.
 */

import type { ChangeKind, FederatedTool } from "../api/types";
import { effectLabel, toolEffect } from "../domain/effect";
import { sourceOf, type Provenance } from "../domain/settings";

export function ToolEffectBadge({
  tool,
}: {
  tool: Pick<FederatedTool, "effect" | "mutates">;
}) {
  const mutating = toolEffect(tool) === "state-changing";
  return (
    <span
      className={`badge badge--${mutating ? "mutating" : "readonly"}`}
      title={
        mutating
          ? "This tool can change something outside CyberFriend. Enabling it requires typing its name."
          : "This tool only reads."
      }
    >
      {mutating ? "⚠ " : "○ "}
      {mutating ? effectLabel(tool.effect) : "read-only"}
    </span>
  );
}

const SOURCE_HELP: Record<Provenance, string> = {
  database: "Set here, and in force.",
  environment:
    "An environment variable is in force. Editing this here will not take effect until that variable is removed.",
  default: "Nothing has set this; the built-in default is in force.",
  unknown:
    "The API reported a provenance this console does not recognise. Treat the value as unexplained rather than as saved.",
};

/**
 * Where a value came from. Without it, a setting edited here but overridden
 * by an environment variable is indistinguishable from one that did not save,
 * and the operator's next move differs completely between the two.
 */
export function SourceBadge({ source }: { source: string }) {
  const provenance = sourceOf(source);
  return (
    <span className={`badge badge--source-${provenance}`} title={SOURCE_HELP[provenance]}>
      {provenance === "unknown" ? `unknown (${source})` : provenance}
    </span>
  );
}

export function YesNoBadge({
  value,
  yes,
  no,
  good = true,
}: {
  value: boolean;
  yes: string;
  no: string;
  /** Whether `true` is the reassuring answer; a channel the bot cannot read is not. */
  good?: boolean;
}) {
  const positive = value === good;
  return (
    <span className={`badge badge--${positive ? "ok" : "warn"}`}>{value ? yes : no}</span>
  );
}

export function KindBadge({ kind }: { kind: ChangeKind }) {
  return <span className={`badge badge--kind-${kind}`}>{kind}</span>;
}
