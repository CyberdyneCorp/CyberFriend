/**
 * Load something from the API, and be honest about the three states.
 *
 * Loading, failed and loaded-but-empty are rendered differently everywhere in
 * this console, because collapsing them is how a screen says "no federated
 * servers" while the request that would have listed them was refused. On a
 * surface whose job is to show what the agent may reach, "nothing" and "we
 * could not ask" must never look the same.
 */

import { useCallback, useEffect, useState } from "react";

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

export function useResource<T>(load: () => Promise<T>, deps: unknown[] = []): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  // The loader is an inline arrow at every call site, so it is a new function
  // on every render; depending on the function itself would refetch forever.
  // The caller's `deps` are what actually decide when to reload.
  const run = useCallback(load, deps);

  useEffect(() => {
    let live = true;
    setLoading(true);
    run().then(
      (value) => {
        if (!live) return;
        setData(value);
        setError(null);
        setLoading(false);
      },
      (cause: unknown) => {
        if (!live) return;
        setError(cause instanceof Error ? cause.message : String(cause));
        setLoading(false);
      },
    );
    return () => {
      live = false;
    };
  }, [run, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}
