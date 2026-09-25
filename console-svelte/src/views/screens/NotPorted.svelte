<!--
  A screen the Svelte console does not have yet. Until the swap, the React
  console in console/ is the one that is built into the image and served.
-->
<script lang="ts">
  import type { ConsoleVM } from "../../viewmodels/console.svelte";
  import Panel from "../components/Panel.svelte";

  // Takes what every screen takes, so it fits the route table; uses none of it.
  // eslint-disable-next-line no-empty-pattern
  let {}: { app: ConsoleVM } = $props();
</script>

<Panel title="Not ported yet">
  <p class="muted">This screen is still served by the current console.</p>
</Panel>
