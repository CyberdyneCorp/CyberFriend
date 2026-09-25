<!--
  A removal that takes two clicks, in place. The armed button names the
  consequence; "Keep" disarms it. See `viewmodels/confirmation.svelte.ts`.
-->
<script lang="ts">
  import { Confirmation } from "../../viewmodels/confirmation.svelte";

  let {
    label,
    confirmLabel,
    busy = false,
    onConfirm,
  }: { label: string; confirmLabel: string; busy?: boolean; onConfirm: () => void } = $props();

  const confirmation = new Confirmation();
</script>

{#if !confirmation.armed}
  <button type="button" class="button button--quiet" disabled={busy} onclick={confirmation.arm}>
    {label}
  </button>
{:else}
  <span class="row row--tight">
    <button
      type="button"
      class="button button--danger"
      disabled={busy}
      onclick={() => confirmation.confirm(onConfirm)}
    >
      {confirmLabel}
    </button>
    <button type="button" class="button button--quiet" onclick={confirmation.disarm}>Keep</button>
  </span>
{/if}
