/**
 * The user area's view-models, wired to the user API once.
 *
 * The user area's counterpart of `ConsoleVM`, and deliberately not part of it:
 * nothing here can reach the console's API, session or screens, and
 * `architecture.test.ts` fails on an import either way.
 */

import type { Erased } from "../../domain/me";
import type { UserApi } from "../../services/userApi";
import { MyFeatureRequestsVM } from "./myFeatureRequests.svelte";
import { PrivacyVM } from "./privacy.svelte";
import { UserSessionVM } from "./userSession.svelte";

export type MeTab = "privacy" | "suggestions";

export class UserAreaVM {
  readonly session: UserSessionVM;
  tab: MeTab = $state("privacy");
  /** What "delete everything" did, shown after the session it ended. */
  erased: Erased | null = $state.raw(null);

  readonly #api: UserApi;

  constructor(api: UserApi) {
    this.#api = api;
    this.session = new UserSessionVM(api);
  }

  privacy(): PrivacyVM {
    return new PrivacyVM(this.#api, () => this.session.me?.fresh === true, this.#finished);
  }

  #finished = (erased: Erased): void => {
    this.erased = erased;
    this.session.ended();
  };

  suggestions(): MyFeatureRequestsVM {
    return new MyFeatureRequestsVM(this.#api);
  }
}
