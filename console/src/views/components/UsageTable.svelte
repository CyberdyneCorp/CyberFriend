<!--
  One grouping of the usage summary. A figure that does not apply to the
  grouping is a dash, not a zero. `onOpen`, when given, offers a person's
  questions; it is given only to an admin signed in as a person.
-->
<script lang="ts">
  import type { UsageGroup, UsageRow } from "../../domain/types";
  import { formatCost, formatCount, formatSeconds } from "../../domain/usage";

  let {
    group,
    rows,
    onOpen,
  }: {
    group: UsageGroup;
    rows: UsageRow[];
    onOpen?: ((row: UsageRow) => void) | undefined;
  } = $props();

  const heading: Record<UsageGroup, string> = {
    person: "Person",
    feature: "Feature",
    model: "Model",
    tool: "Tool",
  };
</script>

{#if rows.length === 0}
  <p class="muted">Nothing traced in this window.</p>
{:else}
  <table class="table">
    <thead>
      <tr>
        <th>{heading[group]}</th>
        <th>Questions</th>
        <th>Input tokens</th>
        <th>Output tokens</th>
        <th>Cost</th>
        <th>Tool calls</th>
        <th>Tools</th>
        <th>Voice</th>
        {#if onOpen}<th></th>{/if}
      </tr>
    </thead>
    <tbody>
      {#each rows as row (row.key)}
        <tr>
          <td>
            {#if group === "person"}
              {row.name ?? row.key}
              {#if row.name !== null}<p class="muted">{row.key}</p>{/if}
            {:else}
              <code>{row.key}</code>
            {/if}
          </td>
          <td>{formatCount(row.questions)}</td>
          <td>{formatCount(row.input_tokens)}</td>
          <td>{formatCount(row.output_tokens)}</td>
          <td>{formatCost(row.cost)}</td>
          <td>{formatCount(row.tool_calls)}</td>
          <td class="cell-value">
            {#if row.tools.length > 0}{row.tools.join(", ")}{:else}<span class="muted">—</span>{/if}
          </td>
          <td>{formatSeconds(row.voice_seconds)}</td>
          {#if onOpen}
            <td class="cell-actions">
              <button type="button" class="button button--quiet" onclick={() => onOpen(row)}>
                Questions
              </button>
            </td>
          {/if}
        </tr>
      {/each}
    </tbody>
  </table>
{/if}
