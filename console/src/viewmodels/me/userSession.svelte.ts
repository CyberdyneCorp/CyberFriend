/**
 * Who is signed in to the user area, and whether a profile is linked.
 *
 * The user area's own session: the `__Host-cf_user` cookie, which the console
 * never reads and which reaches no console route. `start()` asks the API who
 * the cookie names; there is nothing else to hold, because the browser holds
 * the cookie and JavaScript cannot read it.
 */

import type { MeSession } from "../../domain/me";
import type { UserApi } from "../../services/userApi";

export const SIGN_OUT_INCOMPLETE =
  "Sign-out did not reach the server. Your session may still be active; sign in and out again.";

export const UNLINK_FAILED = "Unlinking did not reach the server. The link is still there; try again.";

type SessionApi = Pick<UserApi, "session" | "logout" | "unlink" | "loginUrl">;

export class UserSessionVM {
  me: MeSession | null = $state.raw(null);
  loading = $state(true);
  error: string | null = $state(null);
  /** The link was undone here: the server ended the session with it. */
  unlinked = $state(false);

  readonly signedIn = $derived(this.me !== null);
  readonly linked = $derived(this.me?.linked === true);
  readonly loginUrl: string;

  readonly #api: SessionApi;

  constructor(api: SessionApi) {
    this.#api = api;
    this.loginUrl = api.loginUrl();
  }

  start = async (): Promise<void> => {
    this.loading = true;
    try {
      this.me = await this.#api.session();
      this.error = null;
    } catch (cause) {
      this.error = cause instanceof Error ? cause.message : String(cause);
    } finally {
      this.loading = false;
    }
  };

  /** Forget the session here after the server has ended it, or said it could not. */
  signOut = async (): Promise<void> => {
    try {
      await this.#api.logout();
    } catch {
      this.error = SIGN_OUT_INCOMPLETE;
    }
    this.me = null;
  };

  /**
   * Undo the link: for an account linked to a Discord account that is not the
   * person's own. The server ends the session with it, so this forgets it too.
   */
  unlink = async (): Promise<void> => {
    try {
      await this.#api.unlink();
    } catch {
      this.error = UNLINK_FAILED;
      return;
    }
    this.error = null;
    this.unlinked = true;
    this.me = null;
  };

  /** The server ended the session (an erasure, or a 401): show the sign-in again. */
  ended = (): void => {
    this.me = null;
  };
}
