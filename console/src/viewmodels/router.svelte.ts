/**
 * The hash router: a route table and the route the address names.
 *
 * Routing is by hash because a path router needs the API to rewrite every
 * unknown path to index.html, and a missing rewrite fails as a 404 on
 * refresh, days after the deploy that caused it. The hash asks nothing of the
 * server beyond serving files.
 *
 * Every route states the role it needs. There is no default on purpose,
 * mirroring the server's deny-by-default table; the server stays the
 * authority, and this only decides what the navigation offers and which
 * screen an address may open. An address naming a screen above the signed-in
 * role is treated like an unknown one.
 */

import { allows, type Role } from "../domain/roles";
import type { HashPort } from "../services/hashLocation";

export interface Route<V> {
  path: string;
  title: string;
  view: V;
  minRole: Role;
}

export class Router<V> {
  readonly routes: readonly Route<V>[];
  current: Route<V>;

  readonly #hash: HashPort;
  readonly #fallback: Route<V>;
  readonly #role: () => Role | null;
  #unlisten: (() => void) | null = null;

  constructor(
    routes: readonly Route<V>[],
    hash: HashPort,
    fallbackPath: string,
    role: () => Role | null,
  ) {
    const fallback = routes.find((route) => route.path === fallbackPath);
    if (fallback === undefined) throw new Error(`no route for the fallback ${fallbackPath}`);
    this.routes = routes;
    this.#hash = hash;
    this.#fallback = fallback;
    this.#role = role;
    this.current = $state.raw(this.#resolve());
  }

  /** Follow the address bar until `stop()`. */
  start(): void {
    this.#unlisten ??= this.#hash.listen(() => {
      this.current = this.#resolve();
    });
  }

  stop(): void {
    this.#unlisten?.();
    this.#unlisten = null;
  }

  /** The routes `role` may open, in table order, for the navigation. */
  visibleTo(role: Role | null): Route<V>[] {
    return this.routes.filter((route) => allows(role, route.minRole));
  }

  /** The route the address names; an unknown or out-of-role one is rewritten to the fallback. */
  #resolve(): Route<V> {
    const path = this.#hash.read();
    const found = this.routes.find((route) => route.path === path);
    if (found !== undefined && allows(this.#role(), found.minRole)) return found;
    this.#hash.replace(this.#fallback.path);
    return this.#fallback;
  }
}
