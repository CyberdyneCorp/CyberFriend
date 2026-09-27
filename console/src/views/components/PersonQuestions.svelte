<!--
  One person's questions: their own words, never the answer. Shown to an
  admin signed in as a person; every page read is recorded in the audit.
  Held in memory while this panel is open, and nowhere else.
-->
<script lang="ts">
  import { formatTime } from "../../domain/format";
  import { formatCost, formatCount } from "../../domain/usage";
  import type { QuestionsVM } from "../../viewmodels/usage.svelte";
  import Loaded from "./Loaded.svelte";
  import Panel from "./Panel.svelte";

  let { vm, onClose }: { vm: QuestionsVM; onClose: () => void } = $props();
</script>

<Panel
  title="Questions from {vm.label}"
  description="{vm.window.from} to {vm.window.to}. Only what they asked; answers and quoted messages are never shown. Your viewing is recorded in the audit."
>
  {#snippet actions()}
    <button type="button" class="button button--quiet" onclick={onClose}>Close</button>
  {/snippet}
  <Loaded resource={vm.questions}>
    {#snippet children(found)}
      {#if found.hidden_before_notice > 0}
        <p class="muted">
          {formatCount(found.hidden_before_notice)} question{found.hidden_before_notice === 1 ? "" : "s"}
          traced before this person was told their questions are recorded: counted, never shown.
        </p>
      {/if}
      {#if found.questions.length === 0}
        <p class="muted">No questions to show in this window.</p>
      {:else}
        <table class="table">
          <thead>
            <tr>
              <th>When</th>
              <th>Feature</th>
              <th>Question</th>
              <th>Tools</th>
              <th>Tokens in / out</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {#each found.questions as question, index (`${question.timestamp}-${index}`)}
              <tr>
                <td>{formatTime(question.timestamp)}</td>
                <td><code>{question.feature}</code></td>
                <td class="value">{question.question}</td>
                <td>{question.tools.length > 0 ? question.tools.join(", ") : "—"}</td>
                <td>{formatCount(question.input_tokens)} / {formatCount(question.output_tokens)}</td>
                <td>{formatCost(question.cost)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
      {#if found.total_pages > 1}
        <div class="row">
          <button type="button" class="button button--quiet" disabled={!vm.canPrevious} onclick={vm.previous}>
            Newer
          </button>
          <span class="muted">Page {found.page} of {found.total_pages}</span>
          <button type="button" class="button button--quiet" disabled={!vm.canNext} onclick={vm.next}>
            Older
          </button>
        </div>
      {/if}
    {/snippet}
  </Loaded>
</Panel>
