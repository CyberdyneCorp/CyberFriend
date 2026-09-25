<script lang="ts">
  let { message, onRetry }: { message: string; onRetry?: () => void } = $props();
</script>

<div class="problem" role="alert">
  <span>{message}</span>
  {#if onRetry}
    <button type="button" class="button button--quiet" onclick={onRetry}>Try again</button>
  {/if}
</div>
