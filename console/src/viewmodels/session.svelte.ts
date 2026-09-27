/**
 * Who is signed in, with which role, and the sign-in page's state.
 *
 * Two ways in. When the API offers CyberdyneAuth sign-in, the page shows a
 * "Sign in with CyberdyneAuth" link and the person comes back holding a
 * session cookie; `start()` then asks the API who that is. The operator-token
 * form is the break-glass path: it stands alone when sign-in is not offered,
 * and sits behind "Use an operator token" when it is.
 *
 * A typed token never reaches this class's state after it is checked: the
 * form's text is handed to the session service and cleared. A value that is
 * never in a view-model is never rendered by accident.
 */

import { allows, roleOf, type Role } from "../domain/roles";
import type { Principal } from "../domain/types";
import { REFUSED_MESSAGE } from "../services/http";
import type { SessionPort } from "../services/session";
import type { SessionApi } from "../services/sessionApi";

export const SIGN_OUT_INCOMPLETE =
  "Sign-out did not reach the server. Your session may still be active; sign in and out again.";

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback;
}

export class SessionVM {
  principal: Principal | null = $state(null);
  /** Whether the API offers CyberdyneAuth sign-in; null until it has said. */
  signInOffered: boolean | null = $state(null);
  /** Until the first answer, so the sign-in page does not flash before a session is found. */
  loading = $state(true);
  /** The break-glass form was asked for. */
  tokenRequested = $state(false);
  candidate = $state("");
  error: string | null = $state(null);
  checking = $state(false);

  readonly signedIn = $derived(this.principal !== null);
  /** The highest console role the API reported; the server stays the authority. */
  readonly role: Role | null = $derived(this.principal ? roleOf(this.principal.roles) : null);
  /** Whether to show controls that change something. Hiding them is a courtesy; the API refuses. */
  readonly canChange = $derived(allows(this.role, "admin"));
  readonly viaToken = $derived(this.principal?.via === "token");
  readonly showTokenForm = $derived(this.signInOffered === false || this.tokenRequested);
  readonly canSubmit = $derived(!this.checking && this.candidate.trim() !== "");
  readonly loginUrl: string;

  readonly #session: SessionPort;
  readonly #api: SessionApi;
  readonly #unsubscribe: () => void;

  constructor(session: SessionPort, api: SessionApi) {
    this.#session = session;
    this.#api = api;
    this.loginUrl = api.loginUrl();
    this.principal = session.principal();
    this.#unsubscribe = session.subscribe(() => {
      this.principal = session.principal();
    });
  }

  /** Ask whether sign-in is offered and whether a session cookie already names somebody. */
  start = async (): Promise<void> => {
    this.loading = true;
    const [config, me] = await Promise.all([this.#api.config(), this.#currentSession()]);
    this.signInOffered = config.sign_in;
    // A token typed while this was in flight wins; it was asked for explicitly.
    if (me !== null && this.#session.principal() === null) this.#session.signIn(me);
    this.loading = false;
  };

  useToken = (): void => {
    this.tokenRequested = true;
  };

  /**
   * Check the token, then hold it. In that order: a token that turns out not
   * to work must never have been the session's, or the console renders its
   * shell over a credential it is about to throw away.
   */
  submit = async (): Promise<void> => {
    if (!this.canSubmit) return;
    // Pasted credentials arrive with a newline more often than not.
    const token = this.candidate.trim();
    this.checking = true;
    this.error = null;
    try {
      const who = await this.#api.verifyToken(token);
      this.candidate = "";
      this.#session.signIn(who, token);
    } catch (cause) {
      // Every cause reads the same, matching the API: a refusal that
      // distinguished unknown from revoked would be an enumeration oracle.
      this.error = messageOf(cause, REFUSED_MESSAGE);
    } finally {
      this.checking = false;
    }
  };

  /**
   * Sign out. A token is simply forgotten; a CyberdyneAuth session is revoked
   * at the server first, then the browser goes to the provider's sign-out.
   */
  signOut = async (): Promise<void> => {
    if (this.#session.principal()?.via !== "oidc") {
      this.#session.signOut();
      return;
    }
    let leaveFor: string | null = null;
    try {
      leaveFor = (await this.#api.logout()).end_session_url;
    } catch {
      this.error = SIGN_OUT_INCOMPLETE;
    }
    this.#session.signOut();
    if (leaveFor !== null) this.#api.leave(leaveFor);
  };

  dispose(): void {
    this.#unsubscribe();
  }

  async #currentSession(): Promise<Principal | null> {
    try {
      return await this.#api.me();
    } catch (cause) {
      this.error = messageOf(cause, REFUSED_MESSAGE);
      return null;
    }
  }
}
