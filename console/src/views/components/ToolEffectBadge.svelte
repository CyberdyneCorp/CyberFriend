<!--
  The one component that renders a federated tool's effect, so a tool that
  changes state looks different from one that does not everywhere it appears:
  a server's tool list, the allowlist, and the confirmation that enables it.
  Colour alone is not the signal; the word is there, and so is a glyph.
-->
<script lang="ts">
  import { effectLabel, toolMutates } from "../../domain/effect";
  import type { FederatedTool } from "../../domain/types";

  let { tool }: { tool: Pick<FederatedTool, "effect" | "mutates"> } = $props();

  const mutating = $derived(toolMutates(tool));
</script>

<span
  class="badge badge--{mutating ? 'mutating' : 'readonly'}"
  title={mutating
    ? "This tool can change something outside CyberFriend. Enabling it requires typing its name."
    : "This tool only reads."}
>
  {mutating ? "⚠ " : "○ "}{mutating ? effectLabel(tool.effect) : "read-only"}
</span>
