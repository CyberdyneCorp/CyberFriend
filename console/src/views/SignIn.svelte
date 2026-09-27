<!--
  How somebody gets in.

  With CyberdyneAuth configured, the way in is a link to `auth/login`: the
  admin API runs the whole sign-in and comes back with an httpOnly cookie, so
  no code or token passes through this page. The operator-token form is the
  break-glass path, behind "Use an operator token"; without CyberdyneAuth it
  is the only way in, as it always was.

  A typed token is verified by using it -- one GET /api/session -- and kept in
  this tab's memory only: reloading this page asks again, and the page says so.
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
  <div class="panel">
    <h1>CyberFriend console</h1>
    {#if session.signInOffered}
      <p class="muted">
        Sign in with your CyberdyneAuth account. What you may change depends on the role it grants
        you: operators read, admins change.
      </p>
      <a class="button" href={session.loginUrl}>Sign in with CyberdyneAuth</a>
      {#if !session.showTokenForm}
        <button type="button" class="button button--quiet" onclick={session.useToken}>
          Use an operator token
        </button>
      {/if}
    {/if}
    {#if session.showTokenForm}
      <form class="signin__form" onsubmit={submit}>
        <p class="muted">
          Paste your operator token. It is kept in this tab's memory only — never in browser
          storage — so a reload will ask for it again. That is deliberate: this console can widen
          what the agent may do.
          {#if session.signInOffered}
            With CyberdyneAuth sign-in configured, a token can read but not change anything.
          {/if}
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
        <button type="submit" class="button" disabled={!session.canSubmit}>
          {session.checking ? "Checking…" : "Sign in"}
        </button>
      </form>
    {/if}
    {#if session.error !== null}
      <p class="problem" role="alert">{session.error}</p>
    {/if}
  </div>
</main>
