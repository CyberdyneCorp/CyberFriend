<!--
  What people suggested. Operators read; admins set a status, a note and a
  duplicate link, and every change is recorded in the audit.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import { STATUSES } from "../../domain/featureRequests";
  import { formatTime } from "../../domain/format";
  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import type { StatusFilter } from "../../viewmodels/featureRequests.svelte";
  import ActionResult from "../components/ActionResult.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = app.featureRequests();
  onMount(() => void vm.load());

  const FILTERS: readonly StatusFilter[] = ["all", ...STATUSES];
</script>

<Panel
  title="Feature requests"
  description="What people asked the assistant to learn to do, in their own words."
>
  {#snippet actions()}
    <div class="row row--tight">
      {#each FILTERS as status (status)}
        <button
          type="button"
          class="button button--quiet{vm.filter === status ? ' button--on' : ''}"
          onclick={() => vm.show(status)}
        >
          {status}
        </button>
      {/each}
    </div>
  {/snippet}
  <p class="muted">
    Authors who asked to be told get one direct message per status change. The note is for the
    team and is never sent to them.
  </p>
  <ActionResult action={vm.action} />
  <Loaded resource={vm.requests}>
    {#snippet children(page)}
      {#if page.items.length === 0}
        <p class="muted">No {vm.filter === "all" ? "" : `${vm.filter} `}suggestions.</p>
      {:else}
        <table class="table">
          <thead>
            <tr>
              <th>#</th>
              <th>Suggestion</th>
              <th>From</th>
              <th>Status</th>
              <th>Note</th>
              <th>Received</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each page.items as row (row.id)}
              <tr>
                <td>{row.id}</td>
                <td class="cell-value">
                  {row.text}
                  {#if row.same_text_elsewhere > 0}
                    <p class="muted">also suggested by {row.same_text_elsewhere} other(s)</p>
                  {/if}
                </td>
                <td>{row.person}</td>
                {#if vm.draft !== null && vm.draft.id === row.id}
                  <td>
                    <select aria-label="Status" bind:value={vm.draft.status}>
                      {#each STATUSES as status (status)}
                        <option value={status}>{status}</option>
                      {/each}
                    </select>
                    <input
                      class="input"
                      aria-label="Duplicate of"
                      placeholder="duplicate of #"
                      bind:value={vm.draft.duplicateOf}
                    />
                  </td>
                  <td><textarea aria-label="Note" bind:value={vm.draft.note}></textarea></td>
                  <td>{formatTime(row.created_at)}</td>
                  <td class="cell-actions">
                    <button
                      type="button"
                      class="button"
                      disabled={vm.action.busy}
                      onclick={() => void vm.save(row)}>Save</button
                    >
                    <button type="button" class="button button--quiet" onclick={vm.cancel}>
                      Cancel
                    </button>
                  </td>
                {:else}
                  <td>
                    <span class="badge">{row.status}</span>
                    {#if row.duplicate_of !== null}
                      <p class="muted">of #{row.duplicate_of}</p>
                    {/if}
                  </td>
                  <td class="cell-value">
                    {#if row.admin_note}{row.admin_note}{:else}<span class="muted">—</span>{/if}
                  </td>
                  <td>{formatTime(row.created_at)}</td>
                  <td class="cell-actions">
                    {#if app.session.canChange}
                      <button type="button" class="button button--quiet" onclick={() => vm.edit(row)}>
                        Triage
                      </button>
                    {/if}
                  </td>
                {/if}
              </tr>
            {/each}
          </tbody>
        </table>
        {#if vm.pages > 1}
          <div class="row row--tight">
            <button
              type="button"
              class="button button--quiet"
              disabled={vm.page <= 1}
              onclick={() => vm.turn(-1)}>Newer</button
            >
            <span class="muted">page {vm.page} of {vm.pages}</span>
            <button
              type="button"
              class="button button--quiet"
              disabled={vm.page >= vm.pages}
              onclick={() => vm.turn(1)}>Older</button
            >
          </div>
        {/if}
      {/if}
    {/snippet}
  </Loaded>
</Panel>
