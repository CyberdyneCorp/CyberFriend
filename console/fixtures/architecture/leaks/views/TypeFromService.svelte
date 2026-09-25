<script lang="ts">
  import type { AdminApi } from "../services/adminApi";
  let { api }: { api: AdminApi } = $props();
  void api;
</script>
