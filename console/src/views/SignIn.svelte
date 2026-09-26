<!--
  Where the credential is entered, and the only place it is ever typed.

  The token is verified by using it -- one GET /api/status -- rather than by a
  login endpoint, because there is no session to create. Nothing is stored:
  reloading this page asks again, and the page says so.
-->
<script lang="ts">
  import type { SessionVM } from "../viewmodels/session.svelte";

  let { session }: { session: SessionVM } = $props();

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    void session.submit();
  }
</script>

<main class="signin">
  <form class="panel" onsubmit={submit}>
    <h1>CyberFriend console</h1>
    <p class="muted">
      Paste your operator token. It is kept in this tab's memory only — never in
      browser storage — so a reload will ask for it again. That is deliberate:
      this console can widen what the agent may do.
    </p>
    <label class="field">
      <span>Operator token</span>
      <!-- svelte-ignore a11y_autofocus -->
      <input
        autofocus
        type="password"
        bind:value={session.candidate}
        spellcheck={false}
        autocomplete="off"
        placeholder="cfa_…"
      />
    </label>
    {#if session.error !== null}
      <p class="problem" role="alert">{session.error}</p>
    {/if}
    <button type="submit" class="button" disabled={!session.canSubmit}>
      {session.checking ? "Checking…" : "Sign in"}
    </button>
  </form>
</main>
