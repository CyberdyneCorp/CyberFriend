<!-- Every setting the API exposes, each showing where its value came from. -->
<script lang="ts">
  import { onMount } from "svelte";

  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import Loaded from "../components/Loaded.svelte";
  import Panel from "../components/Panel.svelte";
  import SettingsTable from "../components/SettingsTable.svelte";

  let { app }: { app: ConsoleVM } = $props();

  // svelte-ignore state_referenced_locally
  const vm = app.settings();
  onMount(() => void vm.load());
</script>

<Panel
  title="Settings"
  description="Database beats environment beats default. The source column says which one is in force."
>
  {#snippet actions()}
    <button type="button" class="button button--quiet" onclick={vm.load}>Refresh</button>
  {/snippet}
  <p class="muted">
    Credentials are not here and cannot be set here. The platform token, the model key and the
    database URL are read from the environment; the API refuses to store them and this console
    never displays them, masked or otherwise.
  </p>
  <Loaded resource={vm.settings} empty="The API reports no settings.">
    {#snippet children(rows)}
      <SettingsTable settings={rows} editor={vm.editor} />
    {/snippet}
  </Loaded>
</Panel>
