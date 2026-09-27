<!-- What the last change said, success or refusal. -->
<script lang="ts">
  import type { Action } from "../../viewmodels/action.svelte";

  let { action }: { action: Action } = $props();
</script>

{#if action.error !== null}
  <p class="problem" role="alert">{action.error}</p>
{:else if action.note !== null}
  <p class="note" role="status">{action.note}</p>
{/if}
