<script lang="ts">
  import { formatTime, formatValue } from "../../domain/format";
  import type { Setting } from "../../domain/types";
  import type { SettingEditor } from "../../viewmodels/settings.svelte";
  import ActionResult from "./ActionResult.svelte";
  import SourceBadge from "./SourceBadge.svelte";

  let {
    setting,
    editor,
    canChange,
  }: { setting: Setting; editor: SettingEditor; canChange: boolean } = $props();
</script>

<tr>
  <td>
    <code>{setting.key}</code>
    {#if setting.summary}<p class="muted">{setting.summary}</p>{/if}
  </td>
  <td>
    {#if editor.typed !== null}
      <!-- svelte-ignore a11y_autofocus -->
      <input autofocus class="cell-input" bind:value={editor.typed} spellcheck={false} />
    {:else}
      <span class="value">{formatValue(setting.value)}</span>
    {/if}
    <ActionResult action={editor.action} />
  </td>
  <td>
    <SourceBadge source={setting.source} />
    {#if setting.updated_by}
      <p class="muted">{setting.updated_by} · {formatTime(setting.updated_at)}</p>
    {/if}
  </td>
  <td class="cell-actions">
    {#if !setting.editable}
      <span class="muted">
        {setting.managed_by ? `managed at ${setting.managed_by}` : "not editable here"}
      </span>
    {:else if !canChange}
      <span class="muted">read-only for your role</span>
    {:else if editor.typed !== null}
      <span class="row row--tight">
        <button
          type="button"
          class="button"
          disabled={editor.action.busy}
          onclick={() => void editor.save()}
        >
          Save
        </button>
        <button type="button" class="button button--quiet" onclick={editor.cancel}>Cancel</button>
      </span>
    {:else}
      <button type="button" class="button button--quiet" onclick={() => editor.edit(setting)}>
        Edit
      </button>
    {/if}
  </td>
</tr>
