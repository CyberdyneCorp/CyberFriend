<script lang="ts">
  import { formatValue, humanise } from "../../domain/format";
  import type { Metric } from "../../viewmodels/status.svelte";

  let { entries }: { entries: Metric[] } = $props();
</script>

{#if entries.length === 0}
  <p class="muted">Nothing reported.</p>
{:else}
  <dl class="metrics">
    {#each entries as [key, value] (key)}
      <div class="metric">
        <dt>{humanise(key)}</dt>
        <dd>{formatValue(value)}</dd>
      </div>
    {/each}
  </dl>
{/if}
