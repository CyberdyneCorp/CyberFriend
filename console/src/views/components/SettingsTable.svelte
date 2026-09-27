<!--
  Every setting with its provenance, and an edit box where one is allowed.
  The source badge is on every row, and a row whose value comes from the
  environment says, after a save, that the environment still wins.
-->
<script lang="ts">
  import type { Setting } from "../../domain/types";
  import type { SettingEditor } from "../../viewmodels/settings.svelte";
  import SettingRow from "./SettingRow.svelte";

  let {
    settings,
    editor,
  }: { settings: Setting[]; editor: (key: string) => SettingEditor } = $props();
</script>

<table class="table">
  <thead>
    <tr>
      <th>Setting</th>
      <th>Value</th>
      <th>Source</th>
      <th></th>
    </tr>
  </thead>
  <tbody>
    {#each settings as setting (setting.key)}
      <SettingRow {setting} editor={editor(setting.key)} />
    {/each}
  </tbody>
</table>
