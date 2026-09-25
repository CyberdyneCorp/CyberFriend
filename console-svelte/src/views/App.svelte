<!--
  Sign in, or the screens. Signing out unmounts every screen, so nothing keeps
  rendering data fetched with a credential that is gone.
-->
<script lang="ts">
  import type { ConsoleVM } from "../viewmodels/console.svelte";
  import Shell from "./Shell.svelte";
  import SignIn from "./SignIn.svelte";

  let { app }: { app: ConsoleVM } = $props();
</script>

{#if app.session.signedIn}
  <Shell {app} />
{:else}
  <SignIn session={app.session} />
{/if}
