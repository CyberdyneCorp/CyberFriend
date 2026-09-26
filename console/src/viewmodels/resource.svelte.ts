/**
 * Something loaded from the API, honest about its three states.
 *
 * Loading, failed and loaded-but-empty are rendered differently everywhere in
 * this console, because collapsing them is how a screen says "no federated
 * servers" while the request that would have listed them was refused. On a
 * surface whose job is to show what the agent may reach, "nothing" and "we
 * could not ask" must never look the same.
 *
 * A resource is made to be loaded: it starts as loading, and its view calls
 * `reload()` when it mounts.
 */

function messageOf(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

export class Resource<T> {
  /** Raw, not deep: API payloads are replaced whole, never edited in place. */
  data: T | null = $state.raw(null);
  error: string | null = $state(null);
  loading = $state(true);

  readonly #load: () => Promise<T>;
  /** Only the newest request may write; an older one finishing late is dropped. */
  #generation = 0;

  constructor(load: () => Promise<T>) {
    this.#load = load;
  }

  reload = async (): Promise<void> => {
    const generation = ++this.#generation;
    this.loading = true;
    try {
      const value = await this.#load();
      if (generation !== this.#generation) return;
      this.data = value;
      this.error = null;
    } catch (cause) {
      if (generation !== this.#generation) return;
      this.error = messageOf(cause);
    } finally {
      if (generation === this.#generation) this.loading = false;
    }
  };
}
