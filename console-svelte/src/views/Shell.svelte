<!--
  The signed-in console: navigation and the screen the address names.
-->
<script lang="ts">
  import type { ConsoleVM } from "../viewmodels/console.svelte";
  import { ROUTES } from "./routes";

  let { app }: { app: ConsoleVM } = $props();

  // The console is built once and never replaced, so its first value is the value.
  // svelte-ignore state_referenced_locally
  const router = app.router(ROUTES);
  const nav = $derived(router.visibleTo(app.session.role));

  $effect(() => {
    router.start();
    return () => router.stop();
  });
</script>

<div class="shell">
  <nav class="nav">
    <div class="nav__brand">CyberFriend</div>
    {#each nav as route (route.path)}
      <a
        href="#{route.path}"
        class={route === router.current ? "nav__link nav__link--on" : "nav__link"}
      >
        {route.title}
      </a>
    {/each}
    <button type="button" class="button button--quiet nav__out" onclick={app.session.signOut}>
      Sign out
    </button>
    <p class="nav__note">
      Your token is in this tab's memory only. Closing or reloading it signs you out.
    </p>
  </nav>
  <main class="main">
    {#key router.current}
      <router.current.view {app} />
    {/key}
  </main>
</div>
