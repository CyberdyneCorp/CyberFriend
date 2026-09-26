<!--
  Health, ingestion progress, embedding backlog, and what changed recently.
  The template for every other screen: the view asks the console for its
  view-model, loads it on mount, and only renders what the view-model holds.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import { formatRelative, humanise } from "../../domain/format";
  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import KindBadge from "../components/KindBadge.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Metrics from "../components/Metrics.svelte";
  import Panel from "../components/Panel.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // The console is built once and never replaced, so its first value is the value.
  // svelte-ignore state_referenced_locally
  const vm = app.status();
  onMount(() => void vm.load());
</script>

<Loaded resource={vm.report}>
  {#if vm.health.length > 0}
    <Panel title="Health"><Metrics entries={vm.health} /></Panel>
  {/if}
  {#each vm.groups as group (group.name)}
    <Panel title={humanise(group.name)}>
      <Metrics entries={group.metrics} />
      {#each group.subgroups as subgroup (subgroup.name)}
        <div class="subgroup">
          <h3>{humanise(subgroup.name)}</h3>
          <Metrics entries={subgroup.metrics} />
        </div>
      {/each}
    </Panel>
  {/each}
</Loaded>

<Panel title="Recent changes" description="The last configuration changes, newest first.">
  <Loaded resource={vm.audit} empty="No configuration has been changed yet.">
    <ul class="changes">
      {#each vm.recent as entry, index (`${entry.at}-${entry.setting}-${index}`)}
        <li>
          <KindBadge kind={entry.kind} />
          <span class="changes__setting">{entry.setting}</span>
          <span class="muted">{entry.operator} · {formatRelative(entry.at)}</span>
        </li>
      {/each}
    </ul>
  </Loaded>
</Panel>
