<!--
  The user area: a person's own data, after signing in with CyberdyneAuth.

  Mounted instead of the console when the address is `#/me`. It asks the API
  who the user cookie names and shows nothing until it knows. Signed in but
  not linked, it says how to link; linked, it shows the privacy dashboard and
  the person's suggestions. After "delete everything" it shows what was done,
  and that the CyberdyneAuth account itself is not deleted.
-->
<script lang="ts">
  import { onMount } from "svelte";

  import type { UserAreaVM } from "../../viewmodels/me/userArea.svelte";
  import MePrivacy from "./MePrivacy.svelte";
  import MeSuggestions from "./MeSuggestions.svelte";

  let { area }: { area: UserAreaVM } = $props();

  onMount(() => void area.session.start());
</script>

<main class="me">
  <header class="me__head">
    <h1>Your CyberFriend data</h1>
    {#if area.session.signedIn}
      <p class="muted">
        Signed in as {area.session.me?.email ?? "your CyberdyneAuth account"}.
        <button type="button" class="button button--quiet" onclick={() => void area.session.signOut()}>
          Sign out
        </button>
      </p>
    {/if}
  </header>

  {#if area.erased !== null}
    <section class="panel" role="status">
      <h2>Everything was deleted</h2>
      <p>
        {area.erased.mode === "erase_and_opt_out"
          ? "Everything I held about you was deleted, and I will not archive you again."
          : "Everything I held about you was deleted."}
      </p>
      <p class="muted">{area.erased.identity_account}</p>
    </section>
  {:else if area.session.loading}
    <p class="muted">Checking for a session…</p>
  {:else if !area.session.signedIn}
    <section class="panel">
      <p>Sign in with the CyberdyneAuth account you linked from Discord.</p>
      <a class="button" href={area.session.loginUrl}>Sign in with CyberdyneAuth</a>
      {#if area.session.error !== null}
        <p class="problem" role="alert">{area.session.error}</p>
      {/if}
    </section>
  {:else if !area.session.linked}
    <section class="panel">
      <p>
        No CyberFriend profile is linked to this account. Link one from Discord with
        <code>/account</code>.
      </p>
    </section>
  {:else}
    <nav class="me__tabs">
      <button
        type="button"
        class={area.tab === "privacy" ? "nav__link nav__link--on" : "nav__link"}
        onclick={() => (area.tab = "privacy")}>Privacy</button
      >
      <button
        type="button"
        class={area.tab === "suggestions" ? "nav__link nav__link--on" : "nav__link"}
        onclick={() => (area.tab = "suggestions")}>Suggestions</button
      >
    </nav>
    {#if area.tab === "privacy"}
      <MePrivacy {area} />
    {:else}
      <MeSuggestions {area} />
    {/if}
  {/if}
</main>
