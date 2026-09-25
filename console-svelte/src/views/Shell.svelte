<!--
  The signed-in console: navigation and the screen the address names.
-->
<script lang="ts">
  import type { ConsoleVM } from "../viewmodels/console.svelte";
  import type { Route } from "../viewmodels/router.svelte";
  import { ROUTES, type ScreenView } from "./routes";

  // `routes` is the real table unless a test hands it one with a route the
  // signed-in role may not open.
  let { app, routes = ROUTES }: { app: ConsoleVM; routes?: readonly Route<ScreenView>[] } =
    $props();

  // The console and its table are fixed for the shell's life, so their first values are the values.
  // svelte-ignore state_referenced_locally
  const router = app.router(routes);
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
