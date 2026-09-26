<script lang="ts">
  const load = () => fetch("/api/status");
</script>

<button onclick={load}>Load</button>
