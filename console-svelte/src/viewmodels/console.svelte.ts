/**
 * The console's view-models, wired to their services once.
 *
 * `main.ts` builds one of these with the real services and hands it to the
 * root view; a test builds one with fakes. Views ask it for the view-model of
 * the screen they render, so no view ever needs to name a service.
 */

import type { AdminApi } from "../services/adminApi";
import type { HashPort } from "../services/hashLocation";
import type { SessionPort } from "../services/session";
import { Router, type Route } from "./router.svelte";
import { SessionVM } from "./session.svelte";
import { StatusVM } from "./status.svelte";

/** Where an unknown address lands. */
export const HOME = "/status";

export class ConsoleVM {
  readonly session: SessionVM;

  readonly #api: AdminApi;
  readonly #hash: HashPort;

  constructor(api: AdminApi, session: SessionPort, hash: HashPort) {
    this.#api = api;
    this.#hash = hash;
    this.session = new SessionVM(session, (token) => api.verifyCredential(token));
  }

  router<V>(routes: readonly Route<V>[]): Router<V> {
    return new Router(routes, this.#hash, HOME);
  }

  status(): StatusVM {
    return new StatusVM(this.#api);
  }
}
