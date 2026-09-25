<script lang="ts">
  import { adminApi } from "../services/adminApi";
  const load = () => adminApi.status();
</script>

<button onclick={load}>Load</button>
