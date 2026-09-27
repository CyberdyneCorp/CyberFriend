<!--
  The person's own suggestions, and a form to add one. The server applies the
  rules Discord does, and its refusal is shown as it gives it.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import type { UserAreaVM } from "../../viewmodels/me/userArea.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";

  let { area }: { area: UserAreaVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = area.suggestions();
  onMount(() => void vm.list.reload());

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    void vm.submit();
  }
</script>

<Panel title="Suggest something" description="What should I learn to do? No contact details, please.">
  <form class="signin__form" onsubmit={submit}>
    <label class="field">
      <span>Your suggestion</span>
      <textarea bind:value={vm.draft} rows="3" maxlength="1000"></textarea>
    </label>
    <button type="submit" class="button" disabled={!vm.canSubmit}>
      {vm.submitting.busy ? "Sending…" : "Suggest"}
    </button>
    {#if vm.submitting.note !== null}<p role="status">{vm.submitting.note}</p>{/if}
    {#if vm.submitting.error !== null}<p class="problem" role="alert">{vm.submitting.error}</p>{/if}
  </form>
</Panel>

<Panel title="Your suggestions">
  <Loaded resource={vm.list} empty="You haven't suggested anything yet.">
    {#snippet children(mine)}
      <ul class="changes">
        {#each mine as suggestion (suggestion.id)}
          <li>
            <span>#{suggestion.id} {suggestion.text}</span>
            <span class="muted">{suggestion.status}</span>
          </li>
        {/each}
      </ul>
    {/snippet}
  </Loaded>
</Panel>
