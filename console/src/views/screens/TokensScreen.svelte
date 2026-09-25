<!--
  MCP credentials: review them, and revoke one. There is no issue form, and
  its absence is the point; see `viewmodels/tokens.svelte.ts`.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import { formatTime } from "../../domain/format";
  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import { tokenState } from "../../viewmodels/tokens.svelte";
  import ActionResult from "../components/ActionResult.svelte";
  import ConfirmButton from "../components/ConfirmButton.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = app.tokens();
  onMount(() => void vm.load());
</script>

<Panel
  title="Tokens"
  description="A token is one person's view of the corpus. It never widens what they may read."
>
  {#snippet actions()}
    <button type="button" class="button button--quiet" onclick={vm.load}>Refresh</button>
  {/snippet}
  <p class="muted">
    Credentials are issued from a shell on the host with
    <code>python -m chatmemory.mcp.issue_token</code>. This console can review and revoke them;
    it cannot mint one, because a new credential would read everything that Discord account can
    see.
  </p>
  <ActionResult action={vm.action} />
  <Loaded resource={vm.tokens} empty="No tokens have been issued.">
    {#snippet children(rows)}
      <table class="table">
        <thead>
          <tr>
            <th>Label</th>
            <th>Person</th>
            <th>Issued</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as token, index (token.id ?? `${token.person}-${token.issued_at}-${index}`)}
            {@const state = tokenState(token)}
            <tr>
              <td>
                {#if token.label}{token.label}{:else}<span class="muted">unlabelled</span>{/if}
              </td>
              <td>{token.person}</td>
              <td>{formatTime(token.issued_at)}</td>
              <td>
                {#if state === "revoked"}
                  <span class="badge badge--warn">revoked {formatTime(token.revoked_at)}</span>
                {:else}
                  <span class="badge badge--ok">live</span>
                {/if}
              </td>
              <td class="cell-actions">
                {#if state === "revocable"}
                  <ConfirmButton
                    label="Revoke"
                    confirmLabel="Revoke this token"
                    busy={vm.action.busy}
                    onConfirm={() => void vm.revoke(token)}
                  />
                {:else if state === "no-id"}
                  <span class="muted">no id on this row</span>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/snippet}
  </Loaded>
</Panel>
