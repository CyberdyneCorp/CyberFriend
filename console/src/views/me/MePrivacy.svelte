<!--
  What CyberFriend holds about the signed-in person, and "delete everything".

  The same inventory as `/privacy` in a DM. Messages in server channels are
  counted only in Discord: which channels a person may read is Discord's to
  say, and this page cannot ask it.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import { CONFIRM_WORD, kindLabel } from "../../domain/me";
  import type { UserAreaVM } from "../../viewmodels/me/userArea.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";

  let { area }: { area: UserAreaVM } = $props();

  // The area is built once and never replaced, so its first value is the value.
  // svelte-ignore state_referenced_locally
  const vm = area.privacy();
  onMount(() => void vm.report.reload());

  function erase(event: SubmitEvent): void {
    event.preventDefault();
    void vm.erase();
  }
</script>

<Loaded resource={vm.report}>
  {#snippet children(report)}
    <Panel title="What I hold about you">
      {#if !report.known}
        <p class="muted">Nothing: I have never stored anything about you.</p>
      {/if}
      <p>{report.archiving ? "Your messages are archived." : "You opted out: nothing you send is archived."}</p>
      {#if report.facts.length > 0}
        <dl class="facts">
          {#each report.facts as fact, index (`${fact.kind}-${index}`)}
            <dt>{kindLabel(fact.kind)}</dt>
            <dd>{fact.value}</dd>
          {/each}
        </dl>
      {/if}
      {#if report.recent_questions.length > 0}
        <h3>Your latest questions</h3>
        <ul>
          {#each report.recent_questions as question, index (index)}<li>{question}</li>{/each}
        </ul>
      {/if}
      <p class="muted">
        {report.tasks.length} scheduled questions, {report.alerts.length} alerts,
        {report.suggestions.length} suggestions, {report.tokens.length} tokens,
        {report.traces} traced questions. Messages in server channels are counted in Discord's
        <code>/privacy</code>.
      </p>
    </Panel>
    <Panel title="What is kept">
      <ul>
        {#each vm.retention as line (line)}<li>{line}</li>{/each}
      </ul>
      <p class="muted">{report.identity_account}</p>
    </Panel>
  {/snippet}
</Loaded>

<Panel title="Delete everything" description="Deletes everything I hold about you. It cannot be undone.">
  <form class="signin__form" onsubmit={erase}>
    <label class="field">
      <input type="radio" name="mode" value="erase" bind:group={vm.mode} />
      <span>Delete everything</span>
    </label>
    <label class="field">
      <input type="radio" name="mode" value="erase_and_opt_out" bind:group={vm.mode} />
      <span>Delete everything and stop archiving me</span>
    </label>
    {#if vm.needsFreshSignIn}
      <p class="muted">For your safety, sign in again before deleting.</p>
      <button type="button" class="button" onclick={vm.signInAgain}>Sign in again</button>
    {:else}
      <label class="field">
        <span>Type <code>{CONFIRM_WORD}</code> to confirm</span>
        <input bind:value={vm.typed} spellcheck={false} autocomplete="off" />
      </label>
      <button type="submit" class="button button--danger" disabled={!vm.canErase()}>
        {vm.erasing.busy ? "Deleting…" : "Delete everything"}
      </button>
    {/if}
    {#if vm.erasing.error !== null}
      <p class="problem" role="alert">{vm.erasing.error}</p>
    {/if}
  </form>
</Panel>
