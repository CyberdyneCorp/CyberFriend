<!--
  Sign in, or the screens. Signing out unmounts every screen, so nothing keeps
  rendering data fetched with a credential that is gone.

  On load the console asks the API whether a session cookie already names
  somebody -- a person coming back from CyberdyneAuth holds nothing else --
  and shows nothing until it has the answer, so the sign-in page does not
  flash in front of a live session.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import type { ConsoleVM } from "../viewmodels/console.svelte";
  import Shell from "./Shell.svelte";
  import SignIn from "./SignIn.svelte";

  let { app }: { app: ConsoleVM } = $props();

  onMount(() => void app.session.start());
</script>

{#if app.session.signedIn}
  <Shell {app} />
{:else if app.session.loading}
  <main class="signin"><p class="muted">Checking for a session…</p></main>
{:else}
  <SignIn session={app.session} />
{/if}
