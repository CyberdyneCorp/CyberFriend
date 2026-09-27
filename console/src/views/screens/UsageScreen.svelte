<!--
  What traced questions cost, per person, feature, model and tool. Counts for
  everybody who may open the console; a person's question text only for an
  admin signed in with CyberdyneAuth, and every read of it is audited.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import { formatCost, formatCount, formatSeconds, PRESET_DAYS } from "../../domain/usage";
  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";
  import PersonQuestions from "../components/PersonQuestions.svelte";
  import UsageTable from "../components/UsageTable.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = app.usage();
  onMount(() => void vm.load());

  const open = $derived(app.session.canReadQuestions ? vm.open : undefined);

  function apply(event: SubmitEvent): void {
    event.preventDefault();
    void vm.apply();
  }
</script>

<Panel title="Usage" description="Traced question runs, tokens, estimated cost and tools, per person and per feature.">
  {#snippet actions()}
    <div class="row row--tight">
      {#each PRESET_DAYS as days (days)}
        <button type="button" class="button button--quiet" onclick={() => void vm.preset(days)}>
          Last {days} days
        </button>
      {/each}
    </div>
  {/snippet}
  <form class="form-row" onsubmit={apply}>
    <label class="field">
      <span>From</span>
      <input type="date" bind:value={vm.from} />
    </label>
    <label class="field">
      <span>To</span>
      <input type="date" bind:value={vm.to} />
    </label>
    <button type="submit" class="button" disabled={!vm.canApply}>Show</button>
    {#if vm.problem !== null}<span class="muted" role="status">{vm.problem}</span>{/if}
  </form>
  <div class="muted usage-note">
    <p>
      Totals are traced question runs: background work and people who opted out are never traced,
      so they undercount by design. Cost is an estimate from the trace store's price table. Whole
      UTC days.
    </p>
    <p>
      People who opted out or asked to be erased, everything up to a completed erasure, and traces
      waiting to be deleted are never counted or shown.
      {#if vm.retentionDays !== null}
        Traces are kept {vm.retentionDays} days, then deleted.
      {:else if vm.report.data !== null}
        This service could not read how long traces are kept (<code>TRACE_RETENTION_DAYS</code>).
      {/if}
    </p>
    <p>
      {#if app.session.canReadQuestions}
        A person's questions show only what they asked after they were told their questions are
        recorded; earlier ones are counted. Every view is recorded in the audit under your name.
      {:else}
        Counts only. A person's questions are shown only to an admin signed in with CyberdyneAuth.
      {/if}
    </p>
  </div>
  <Loaded resource={vm.report}>
    {#snippet children(report)}
      <p class="muted">{report.person.from} to {report.person.to}</p>
      <dl class="metrics">
        <div class="metric"><dt>Traced question runs</dt><dd>{formatCount(report.person.totals.questions)}</dd></div>
        <div class="metric"><dt>Input tokens</dt><dd>{formatCount(report.person.totals.input_tokens)}</dd></div>
        <div class="metric"><dt>Output tokens</dt><dd>{formatCount(report.person.totals.output_tokens)}</dd></div>
        <div class="metric"><dt>Estimated cost</dt><dd>{formatCost(report.person.totals.cost)}</dd></div>
        <div class="metric"><dt>Tool calls</dt><dd>{formatCount(report.person.totals.tool_calls)}</dd></div>
        <div class="metric"><dt>Voice</dt><dd>{formatSeconds(report.person.totals.voice_seconds)}</dd></div>
      </dl>
    {/snippet}
  </Loaded>
</Panel>

{#if vm.questions !== null}
  {#key vm.questions}
    <PersonQuestions vm={vm.questions} onClose={vm.close} />
  {/key}
{/if}

{#if vm.report.data !== null && vm.report.error === null}
  <Panel title="By person">
    <UsageTable group="person" rows={vm.report.data.person.rows} onOpen={open} />
  </Panel>
  <Panel title="By feature">
    <UsageTable group="feature" rows={vm.report.data.feature.rows} />
  </Panel>
  <Panel title="By model">
    <UsageTable group="model" rows={vm.report.data.model.rows} />
  </Panel>
  <Panel title="By tool">
    <UsageTable group="tool" rows={vm.report.data.tool.rows} />
  </Panel>
{/if}
