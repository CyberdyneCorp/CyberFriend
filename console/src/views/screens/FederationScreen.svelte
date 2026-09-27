<!--
  What the agent may reach outside itself, and what it may do when it gets
  there. The rules live in `viewmodels/federation.svelte.ts`; this renders them.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import { toolMutates } from "../../domain/effect";
  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import ActionResult from "../components/ActionResult.svelte";
  import ConfirmButton from "../components/ConfirmButton.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";
  import ServerCard from "../components/ServerCard.svelte";
  import ToolEffectBadge from "../components/ToolEffectBadge.svelte";
  import TypedConfirm from "../components/TypedConfirm.svelte";
  import YesNoBadge from "../components/YesNoBadge.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = app.federation();
  const servers = vm.servers;
  onMount(() => void vm.load());

  function addServer(event: SubmitEvent): void {
    event.preventDefault();
    void servers.add();
  }
</script>

<Panel
  title="Federated servers"
  description="Added here after a probe. An unreachable server offers the agent nothing."
>
  {#snippet actions()}
    <button type="button" class="button button--quiet" onclick={vm.refresh}>Refresh</button>
  {/snippet}
  <form class="form-row" onsubmit={addServer}>
    <label class="field">
      <span>Name</span>
      <input bind:value={servers.name} spellcheck={false} />
    </label>
    <label class="field field--wide">
      <span>Target</span>
      <input bind:value={servers.target} spellcheck={false} placeholder="https://…" />
    </label>
    <button type="submit" class="button" disabled={!servers.canAdd}>
      {servers.adding.busy ? "Probing…" : "Add server"}
    </button>
    <ActionResult action={servers.adding} />
  </form>
  <ActionResult action={vm.action} />
  {#if vm.pending !== null}
    <TypedConfirm
      gate={vm.pending}
      busy={vm.action.busy}
      onConfirm={() => void vm.confirm()}
      onCancel={vm.cancel}
    />
  {/if}
  <Loaded
    resource={servers.servers}
    empty="No federated servers. The agent reaches nothing outside itself."
  >
    {#snippet children(list)}
      <div class="cards">
        {#each list as server (server.name)}
          <ServerCard
            {server}
            allowed={vm.allowlist.keys}
            busy={vm.action.busy}
            onAllow={(choice) => void vm.choose(choice)}
            onRemove={() => void vm.removeServer(server)}
          />
        {/each}
      </div>
    {/snippet}
  </Loaded>
</Panel>

<Panel
  title="Allowlist"
  description="What the agent may actually call. Anything not listed here is not offered to it."
>
  <Loaded resource={vm.allowlist.allowlist} empty="Nothing is allowlisted.">
    {#snippet children(entries)}
      <table class="table">
        <thead>
          <tr>
            <th>Server</th>
            <th>Tool</th>
            <th>Effect</th>
            <th>Mutation</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each entries as entry (`${entry.server}/${entry.tool}`)}
            <tr class={toolMutates(entry) ? "row--mutating" : undefined}>
              <td>{entry.server}</td>
              <td><code>{entry.tool}</code></td>
              <td><ToolEffectBadge tool={entry} /></td>
              <td>
                <YesNoBadge
                  value={entry.mutation_enabled}
                  yes="enabled"
                  no="read-only"
                  good={false}
                />
              </td>
              <td class="cell-actions">
                <ConfirmButton
                  label="Revoke"
                  confirmLabel="Revoke this tool"
                  busy={vm.action.busy}
                  onConfirm={() => void vm.revoke(entry)}
                />
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/snippet}
  </Loaded>
</Panel>
