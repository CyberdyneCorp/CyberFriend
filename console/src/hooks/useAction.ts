/**
 * Run one change, and keep what the API said about it.
 *
 * The result is kept and shown rather than swallowed because the interesting
 * answers here are the ones that are not "done": a channel added that the bot
 * cannot read, a tool refused because no configured server offers it. Those
 * come back from a call that succeeded, and a screen that only rendered
 * failures would drop them.
 */

import { useCallback, useState } from "react";

export interface Action {
  run: (work: () => Promise<string | void>) => void;
  busy: boolean;
  error: string | null;
  note: string | null;
  clear: () => void;
}

export function useAction(): Action {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const run = useCallback((work: () => Promise<string | void>) => {
    setBusy(true);
    setError(null);
    setNote(null);
    work().then(
      (message) => {
        setNote(typeof message === "string" ? message : null);
        setBusy(false);
      },
      (cause: unknown) => {
        setError(cause instanceof Error ? cause.message : String(cause));
        setBusy(false);
      },
    );
  }, []);

  const clear = useCallback(() => {
    setError(null);
    setNote(null);
  }, []);

  return { run, busy, error, note, clear };
}
