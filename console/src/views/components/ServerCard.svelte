<!--
  One federated server: whether it answered, and what it offers.

  Reachability is shown per server and never inferred from an empty tool list:
  a server that is down and one that offers nothing are different problems.
  A server's claim to be read-only is shown next to the effect the agent will
  actually treat the tool as having, because the claim grants nothing.
-->
<script lang="ts">
  import { isAllowlisted } from "../../domain/allowlist";
  import { toolMutates } from "../../domain/effect";
  import type { FederationServer } from "../../domain/types";
  import type { ToolChoice } from "../../viewmodels/federation.svelte";
  import ConfirmButton from "./ConfirmButton.svelte";
  import ToolEffectBadge from "./ToolEffectBadge.svelte";
  import YesNoBadge from "./YesNoBadge.svelte";

  let {
    server,
    allowed,
    busy,
    onAllow,
    onRemove,
  }: {
    server: FederationServer;
    /** `server/tool` keys already on the allowlist, for an API that sent no flag. */
    allowed: ReadonlySet<string>;
    busy: boolean;
    onAllow: (choice: ToolChoice) => void;
    onRemove: () => void;
  } = $props();
</script>

<article class="card">
  <header class="card__head">
    <div>
      <h3>{server.name}</h3>
      <p class="muted"><code>{server.target}</code></p>
    </div>
    <div class="row row--tight">
      <YesNoBadge value={server.reachable} yes="reachable" no="unreachable" />
      <ConfirmButton
        label="Remove"
        confirmLabel="Remove this server"
        {busy}
        onConfirm={onRemove}
      />
    </div>
  </header>
  {#if server.failure}<p class="problem">{server.failure}</p>{/if}
  {#if server.tools.length === 0}
    <p class="muted">
      {server.reachable
        ? "Reachable, and offers no tools."
        : "No tools discovered; the last probe did not reach it."}
    </p>
  {:else}
    <ul class="tools">
      {#each server.tools as tool (tool.name)}
        {@const mutates = toolMutates(tool)}
        <li class={mutates ? "tool tool--mutating" : "tool"}>
          <span class="tool__name">{tool.name}</span>
          <ToolEffectBadge {tool} />
          {#if tool.claims_read_only && mutates}
            <span class="muted">the server claims read-only</span>
          {/if}
          {#if isAllowlisted(server.name, tool, allowed)}
            <span class="muted">
              {tool.mutation_enabled ? "allowed, mutation enabled" : "on the allowlist"}
            </span>
          {:else}
            <button
              type="button"
              class="button button--quiet"
              disabled={busy}
              onclick={() => onAllow({ server: server.name, tool })}
            >
              Allow
            </button>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</article>
