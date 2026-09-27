<script lang="ts">
  // Shared building blocks are fine from either side.
  import Panel from "../components/Panel.svelte";
  import { Resource } from "../../viewmodels/resource.svelte";
  import type { UserAreaVM } from "../../viewmodels/me/userArea.svelte";

  let { area }: { area: UserAreaVM } = $props();
  const r = new Resource(async () => 1);
</script>

<Panel title="x">{String(area)}{String(r)}</Panel>
