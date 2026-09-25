<script lang="ts">
  import type { Snippet } from "svelte";

  let {
    title,
    description,
    actions,
    children,
  }: { title: string; description?: string; actions?: Snippet; children: Snippet } = $props();
</script>

<section class="panel">
  <header class="panel__head">
    <div>
      <h2>{title}</h2>
      {#if description}<p class="muted">{description}</p>{/if}
    </div>
    {#if actions}<div class="panel__actions">{@render actions()}</div>{/if}
  </header>
  {@render children()}
</section>
