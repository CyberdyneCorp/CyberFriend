/**
 * Signed in or not, and the sign-in form's state.
 *
 * The token itself never reaches this class's state: the form's text is held
 * until it is checked and then handed to the session service, and nothing
 * here exposes it afterwards. A value that is never in a view-model is never
 * rendered by accident.
 */

import type { Role } from "../domain/roles";
import { REFUSED_MESSAGE } from "../services/http";
import type { SessionPort } from "../services/session";

export class SessionVM {
  signedIn = $state(false);
  candidate = $state("");
  error: string | null = $state(null);
  checking = $state(false);

  readonly canSubmit = $derived(!this.checking && this.candidate.trim() !== "");

  /**
   * The console role of whoever is signed in. A bearer token is admin until
   * the CyberdyneAuth login change gives the session a real principal; the
   * server stays the authority either way.
   */
  readonly role: Role | null = $derived(this.signedIn ? "admin" : null);

  readonly #session: SessionPort;
  readonly #verify: (token: string) => Promise<void>;
  readonly #unsubscribe: () => void;

  constructor(session: SessionPort, verify: (token: string) => Promise<void>) {
    this.#session = session;
    this.#verify = verify;
    this.signedIn = session.isSignedIn();
    this.#unsubscribe = session.subscribe(() => {
      this.signedIn = session.isSignedIn();
    });
  }

  /**
   * Check the credential, then hold it. In that order: a token that turns out
   * not to work must never have been the session's, or the console renders
   * its shell over a credential it is about to throw away.
   */
  submit = async (): Promise<void> => {
    if (!this.canSubmit) return;
    // Pasted credentials arrive with a newline more often than not.
    const token = this.candidate.trim();
    this.checking = true;
    this.error = null;
    try {
      await this.#verify(token);
      this.candidate = "";
      this.#session.signIn(token);
    } catch (cause) {
      // Every cause reads the same, matching the API: a refusal that
      // distinguished unknown from revoked would be an enumeration oracle.
      this.error = cause instanceof Error && cause.message ? cause.message : REFUSED_MESSAGE;
    } finally {
      this.checking = false;
    }
  };

  signOut = (): void => {
    this.#session.signOut();
  };

  dispose(): void {
    this.#unsubscribe();
  }
}
