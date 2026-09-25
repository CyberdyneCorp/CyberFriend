<script lang="ts">
  let remember = $state(false);
</script>

<label>
  <input type="checkbox" bind:checked={remember} />
  Remember me
</label>
<button onclick={() => remember && localStorage.setItem("token", "pasted")}>Sign in</button>
