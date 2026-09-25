/**
 * The address bar, as the router sees it: the part after `#`.
 *
 * A port rather than `window.location` read inside the router, so the router
 * is a view-model a node test can drive.
 */

export interface HashPort {
  /** The path after `#`, e.g. `/status`; empty when there is none. */
  read: () => string;
  /** Point the address at `path` without adding a history entry. */
  replace: (path: string) => void;
  /** Called on every change; returns the unsubscribe. */
  listen: (listener: () => void) => () => void;
}

export const browserHash: HashPort = {
  read: () => window.location.hash.replace(/^#/, ""),
  replace: (path) => {
    const url = new URL(window.location.href);
    url.hash = path;
    window.history.replaceState(window.history.state, "", url);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  },
  listen: (listener) => {
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  },
};
