/**
 * Where the credential is entered, and the only place it is ever typed.
 *
 * The token is verified by using it -- one GET /api/status -- rather than by
 * a login endpoint, because there is no session to create. Nothing is stored:
 * reloading this page asks again, and the page says so, so that an operator
 * who expected to be remembered knows it is the design rather than a bug.
 */

import { useState } from "react";

import { REFUSED_MESSAGE, verifyCredential } from "../api/client";
import { signIn } from "../auth/session";

export function SignIn() {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  async function attempt(candidate: string): Promise<void> {
    try {
      // Checked before it is held: a token that turns out not to work must
      // never have been the session's, or the console renders its shell over
      // a credential it is about to throw away.
      await verifyCredential(candidate);
      signIn(candidate);
    } catch (cause) {
      // Every cause reads the same, matching the API: a refusal that
      // distinguished unknown from revoked would tell whoever holds one dead
      // token which names still have live ones.
      setError(cause instanceof Error && cause.message ? cause.message : REFUSED_MESSAGE);
    } finally {
      setChecking(false);
    }
  }

  return (
    <main className="signin">
      <form
        className="panel"
        onSubmit={(event) => {
          event.preventDefault();
          if (token.trim() === "") return;
          setChecking(true);
          setError(null);
          void attempt(token);
        }}
      >
        <h1>CyberFriend console</h1>
        <p className="muted">
          Paste your operator token. It is kept in this tab's memory only —
          never in browser storage — so a reload will ask for it again. That is
          deliberate: this console can widen what the agent may do.
        </p>
        <label className="field">
          <span>Operator token</span>
          <input
            autoFocus
            type="password"
            value={token}
            spellCheck={false}
            autoComplete="off"
            placeholder="cfa_…"
            onChange={(event) => setToken(event.target.value)}
          />
        </label>
        {error !== null ? (
          <p className="problem" role="alert">
            {error}
          </p>
        ) : null}
        <button type="submit" className="button" disabled={checking || token.trim() === ""}>
          {checking ? "Checking…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
