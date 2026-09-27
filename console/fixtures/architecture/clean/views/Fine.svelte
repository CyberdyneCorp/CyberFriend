<!-- import { adminApi } from "../services/adminApi"; is prose, not code. -->
<script lang="ts">
  // fetch("/api/status") in a comment is not a request either.
  import { formatValue } from "../domain/format";
  import type { StatusVM } from "../viewmodels/status.svelte";
  import Panel from "./components/Panel.svelte";

  let { vm }: { vm: StatusVM } = $props();
</script>

<Panel title="x">{formatValue(vm.health.length)}</Panel>
