/**
 * One change, and what the API said about it.
 *
 * The result is kept and shown rather than swallowed because the interesting
 * answers here are the ones that are not "done": a channel added that the bot
 * cannot read, a tool refused because no configured server offers it. Those
 * come back from a call that succeeded, and a screen that only rendered
 * failures would drop them -- hence `note` beside `error`.
 */

export class Action {
  busy = $state(false);
  error: string | null = $state(null);
  note: string | null = $state(null);

  /** Resolves when the work settles; it never rejects, the error is state. */
  run = async (work: () => Promise<string | void>): Promise<void> => {
    this.busy = true;
    this.clear();
    try {
      const message = await work();
      this.note = typeof message === "string" ? message : null;
    } catch (cause) {
      this.error = cause instanceof Error ? cause.message : String(cause);
    } finally {
      this.busy = false;
    }
  };

  clear = (): void => {
    this.error = null;
    this.note = null;
  };
}
