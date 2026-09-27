<!--
  The gate in front of enabling a tool that changes state.

  A typed confirmation and not a dialog, because a dialog is a thing people
  click through. The operator reads the name of the specific tool they are
  granting and types it back; nothing else enables the button. The API refuses
  a mismatched `confirm_tool_name` too -- this is the first of two gates.

  The effect badge is repeated inside the gate deliberately: the confirmation
  is the moment to say again, in the same visual language, what kind of tool
  it is.
-->
<script lang="ts">
  import type { TypedConfirmVM } from "../../viewmodels/federation.svelte";
  import ToolEffectBadge from "./ToolEffectBadge.svelte";

  let {
    gate,
    busy,
    onConfirm,
    onCancel,
  }: { gate: TypedConfirmVM; busy: boolean; onConfirm: () => void; onCancel: () => void } =
    $props();

  const name = $derived(gate.choice.tool.name);
</script>

<div class="confirm" role="group" aria-label="Enable {name}">
  <p>
    <strong>{name}</strong> on <strong>{gate.choice.server}</strong>
    <ToolEffectBadge tool={gate.choice.tool} />
  </p>
  <p class="muted">
    Enabling this lets the agent change something outside CyberFriend while answering a
    question. Type the tool's name to enable it. This is recorded as an escalation against
    your name.
  </p>
  <label class="field">
    <span>Type <code>{name}</code></span>
    <!-- svelte-ignore a11y_autofocus -->
    <input
      autofocus
      bind:value={gate.typed}
      spellcheck={false}
      autocomplete="off"
      aria-invalid={gate.typed !== "" && !gate.matches}
    />
  </label>
  <div class="row">
    <button
      type="button"
      class="button button--danger"
      disabled={!gate.canConfirm(busy)}
      onclick={onConfirm}
    >
      {busy ? "Enabling…" : "Enable this tool"}
    </button>
    <button type="button" class="button button--quiet" onclick={onCancel}>Cancel</button>
  </div>
</div>
