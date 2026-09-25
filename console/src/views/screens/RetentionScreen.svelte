<!--
  How long anything is kept, and who has asked to be left out of it: what the
  archive forgets on a clock, and what it never records in the first place.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import { formatTime } from "../../domain/format";
  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import ActionResult from "../components/ActionResult.svelte";
  import ConfirmButton from "../components/ConfirmButton.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";
  import SettingsTable from "../components/SettingsTable.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = app.retention();
  onMount(() => void vm.load());

  function add(event: SubmitEvent): void {
    event.preventDefault();
    void vm.add();
  }
</script>

<Panel title="Retention" description="How long messages, windows, asks and documents are kept.">
  <Loaded resource={vm.settings.settings}>
    {#if vm.retention.length === 0}
      <p class="muted">
        The API exposes no retention setting, so retention is whatever the deployment configured.
        Everything said in an indexed channel is kept until then.
      </p>
    {:else}
      <SettingsTable
        settings={vm.retention}
        editor={vm.settings.editor}
        canChange={app.session.canChange}
      />
    {/if}
  </Loaded>
</Panel>

<Panel title="Opt-outs" description="People whose messages are not recorded and not retrievable.">
  {#if app.session.canChange}
    <form class="form-row" onsubmit={add}>
      <label class="field">
        <span>Platform</span>
        <input bind:value={vm.platform} />
      </label>
      <label class="field">
        <span>Platform user id</span>
        <input bind:value={vm.userId} spellcheck={false} placeholder="e.g. 1234567890" />
      </label>
      <button type="submit" class="button" disabled={!vm.canAdd}>
        {vm.adding.busy ? "Saving…" : "Opt this person out"}
      </button>
      <ActionResult action={vm.adding} />
    </form>
  {/if}
  <ActionResult action={vm.action} />
  <Loaded resource={vm.optOuts} empty="Nobody has opted out.">
    <table class="table">
      <thead>
        <tr>
          <th>Person</th>
          <th>Since</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each vm.rows as row (row.entry.person)}
          <tr>
            <td>{row.entry.person}</td>
            <td>{formatTime(row.entry.since)}</td>
            <td class="cell-actions">
              {#if row.person === null}
                <span class="muted">this row cannot be read as a person</span>
              {:else if app.session.canChange}
                <ConfirmButton
                  label="Opt back in"
                  confirmLabel="Record them again"
                  busy={vm.action.busy}
                  onConfirm={() => void vm.optBackIn(row)}
                />
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </Loaded>
</Panel>
