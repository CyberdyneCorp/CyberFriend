<!--
  The token lives in this component's state only. It is never written to
  localStorage, sessionStorage or document.cookie.
-->
<script lang="ts">
  // Not persisted: see the note above about localStorage.
  let token = $state("");
</script>

<!-- A reload signs the operator out; localStorage would not. -->
<input type="password" bind:value={token} />
