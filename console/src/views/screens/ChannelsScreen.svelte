<!--
  What is indexed, and whether the bot can actually read it: two different
  facts, kept apart, because a channel in scope that the bot cannot read
  indexes nothing and says so nowhere else.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import ActionResult from "../components/ActionResult.svelte";
  import ConfirmButton from "../components/ConfirmButton.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";
  import YesNoBadge from "../components/YesNoBadge.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = app.channels();
  onMount(() => void vm.load());

  function add(event: SubmitEvent): void {
    event.preventDefault();
    void vm.add();
  }
</script>

<Panel
  title="Channels"
  description="Everything said in an indexed channel becomes searchable, subject to who may read it."
>
  {#snippet actions()}
    <button type="button" class="button button--quiet" onclick={vm.load}>Refresh</button>
  {/snippet}
  <form class="form-row" onsubmit={add}>
    <label class="field">
      <span>Channel id</span>
      <input bind:value={vm.id} spellcheck={false} placeholder="e.g. 1234567890" />
    </label>
    <button type="submit" class="button" disabled={!vm.canAdd}>
      {vm.adding.busy ? "Checking…" : "Index this channel"}
    </button>
    <ActionResult action={vm.adding} />
  </form>
  <ActionResult action={vm.action} />
  <Loaded resource={vm.channels} empty="No channels are indexed.">
    {#snippet children(rows)}
      <table class="table">
        <thead>
          <tr>
            <th>Channel</th>
            <th>Id</th>
            <th>Indexed</th>
            <th>Bot access</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as channel (channel.id)}
            <tr>
              <td>
                {#if channel.name}{channel.name}{:else}<span class="muted">unnamed</span>{/if}
              </td>
              <td><code>{channel.id}</code></td>
              <td><YesNoBadge value={channel.indexed} yes="indexed" no="not indexed" /></td>
              <td>
                <YesNoBadge value={channel.readable_by_bot} yes="can read" no="cannot read" />
                {#if channel.evidence}<p class="muted">{channel.evidence}</p>{/if}
              </td>
              <td class="cell-actions">
                <ConfirmButton
                  label="Stop indexing"
                  confirmLabel="Stop indexing it"
                  busy={vm.action.busy}
                  onConfirm={() => void vm.remove(channel)}
                />
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/snippet}
  </Loaded>
</Panel>
