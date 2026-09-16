/** The signed-in state, read from the in-memory session store. */

import { useSyncExternalStore } from "react";

import { isSignedIn, subscribe } from "../auth/session";

export function useSignedIn(): boolean {
  // The token itself is never returned to a component: nothing renders it,
  // and a value that is never in a prop is never in a React devtools tree.
  return useSyncExternalStore(subscribe, isSignedIn, () => false);
}
