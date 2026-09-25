<!--
  Who changed what, when, and from what to what. Append-only: nothing in this
  console can edit or remove an entry.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import { formatTime } from "../../domain/format";
  import { FILTERS } from "../../viewmodels/audit.svelte";
  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import KindBadge from "../components/KindBadge.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = app.audit();
  onMount(() => void vm.load());
</script>

<Panel title="Audit" description="Append-only. Nothing in this console can edit or remove an entry.">
  {#snippet actions()}
    <div class="row row--tight">
      {#each FILTERS as kind (kind)}
        <button
          type="button"
          class="button button--quiet{vm.filter === kind ? ' button--on' : ''}"
          onclick={() => vm.show(kind)}
        >
          {kind}
        </button>
      {/each}
    </div>
  {/snippet}
  <Loaded resource={vm.audit} empty="Nothing has been changed yet.">
    {#if vm.shown.length === 0}
      <p class="muted">No {vm.filter} changes.</p>
    {:else}
      <table class="table">
        <thead>
          <tr>
            <th>When</th>
            <th>Operator</th>
            <th>Setting</th>
            <th>Before</th>
            <th>After</th>
            <th>Kind</th>
          </tr>
        </thead>
        <tbody>
          {#each vm.shown as entry, index (`${entry.at}-${entry.setting}-${index}`)}
            <tr class={entry.kind === "escalation" ? "row--mutating" : undefined}>
              <td>{formatTime(entry.at)}</td>
              <td>{entry.operator}</td>
              <td>
                <code>{entry.setting}</code>
                {#if entry.reason}<p class="muted">{entry.reason}</p>{/if}
              </td>
              <td class="cell-value">
                {#if entry.before !== null}{entry.before}{:else}<span class="muted">—</span>{/if}
              </td>
              <td class="cell-value">
                {#if entry.after !== null}{entry.after}{:else}<span class="muted">—</span>{/if}
              </td>
              <td><KindBadge kind={entry.kind} /></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </Loaded>
</Panel>
