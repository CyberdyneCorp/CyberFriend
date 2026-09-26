<script lang="ts">
  import type { ChangeKind } from "../../domain/types";

  let { kind }: { kind: ChangeKind } = $props();
</script>

<span class="badge badge--kind-{kind}">{kind}</span>
