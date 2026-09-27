/**
 * What traced questions cost, per person, feature, model and tool, and -- for
 * an admin signed in as a person -- what one person asked.
 *
 * Every grouping is one read of the same cached rows on the server, so the
 * four tables are loaded together and always describe the same window. The
 * inputs are not that window until they are applied: a table under a date
 * range it was not read for would say something false.
 *
 * Question text is held only here, in memory, for as long as its panel is
 * open. Closing the panel, changing the window or leaving the screen drops
 * it, and nothing writes it anywhere else.
 */

import { lastDays, DEFAULT_WINDOW_DAYS, windowProblem } from "../domain/usage";
import type {
  PersonQuestions,
  UsageGroup,
  UsageRow,
  UsageSummary,
  UsageWindow,
} from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { ApiError } from "../services/http";
import { Resource } from "./resource.svelte";

/** What the screen says when the trace store cannot be read (the API's 503). */
export const UNAVAILABLE =
  "Usage is unavailable: the trace store could not be read. Nothing is shown rather than stale or partial figures.";

export type UsageReport = Record<UsageGroup, UsageSummary>;

type UsageApi = Pick<AdminApi, "usageSummary" | "personQuestions">;

/** The API's 503 as one sentence; anything else as the API said it. */
async function orUnavailable<T>(read: () => Promise<T>): Promise<T> {
  try {
    return await read();
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 503) throw new Error(UNAVAILABLE);
    throw cause;
  }
}

export class QuestionsVM {
  readonly userId: string;
  readonly label: string;
  readonly window: UsageWindow;
  page = $state(1);
  readonly questions: Resource<PersonQuestions>;
  readonly canPrevious: boolean;
  readonly canNext: boolean;

  constructor(api: UsageApi, row: UsageRow, window: UsageWindow) {
    this.userId = row.key;
    this.label = row.name ?? row.key;
    this.window = window;
    this.questions = new Resource(() =>
      orUnavailable(() => api.personQuestions(this.userId, this.window, this.page)),
    );
    this.canPrevious = $derived(!this.questions.loading && this.page > 1);
    this.canNext = $derived(
      !this.questions.loading && this.page < (this.questions.data?.total_pages ?? 0),
    );
  }

  load = (): Promise<void> => this.questions.reload();

  next = (): Promise<void> => this.#turn(this.canNext, 1);

  previous = (): Promise<void> => this.#turn(this.canPrevious, -1);

  #turn(allowed: boolean, step: number): Promise<void> {
    if (!allowed) return Promise.resolve();
    this.page += step;
    return this.load();
  }
}

export class UsageVM {
  /** The date inputs, as typed. */
  from = $state("");
  to = $state("");
  /** The window the tables were read for. */
  shown: UsageWindow = $state.raw({ from: "", to: "" });
  readonly report: Resource<UsageReport>;
  readonly problem: string | null;
  readonly canApply: boolean;
  /** How long traces are kept, as the API reports it; null when unknown. */
  readonly retentionDays: number | null;
  /** One person's questions, while their panel is open. */
  questions: QuestionsVM | null = $state.raw(null);

  readonly #api: UsageApi;
  readonly #canReadQuestions: () => boolean;
  readonly #now: () => number;

  constructor(api: UsageApi, canReadQuestions: () => boolean, now: () => number = Date.now) {
    this.#api = api;
    this.#canReadQuestions = canReadQuestions;
    this.#now = now;
    this.#setInputs(lastDays(DEFAULT_WINDOW_DAYS, now()));
    this.shown = { from: this.from, to: this.to };
    this.report = new Resource(() => orUnavailable(() => this.#read(this.shown)));
    this.problem = $derived(windowProblem({ from: this.from, to: this.to }));
    // Not held back while a read is in flight: a newer one supersedes it, and
    // Resource drops the older answer, so the tables follow the last choice.
    this.canApply = $derived(this.problem === null);
    this.retentionDays = $derived(this.report.data?.person.retention_days ?? null);
  }

  load = (): Promise<void> => this.report.reload();

  /** Read the tables for the typed window. Closes any open questions: they were for the old one. */
  apply = async (): Promise<void> => {
    if (!this.canApply) return;
    this.shown = { from: this.from, to: this.to };
    this.close();
    await this.report.reload();
  };

  /** The last `days` days, read at once. */
  preset = async (days: number): Promise<void> => {
    this.#setInputs(lastDays(days, this.#now()));
    await this.apply();
  };

  /**
   * Open a person's questions, for the window the visible table was read for.
   * Offered to an admin signed in as a person only, and refused while a new
   * window is loading: the row on screen belongs to the old one.
   */
  open = async (row: UsageRow): Promise<void> => {
    const read = this.report.data?.person;
    if (!this.#canReadQuestions() || this.report.loading || read === undefined) return;
    const questions = new QuestionsVM(this.#api, row, { from: read.from, to: read.to });
    this.questions = questions;
    await questions.load();
  };

  close = (): void => {
    this.questions = null;
  };

  #setInputs(window: UsageWindow): void {
    this.from = window.from;
    this.to = window.to;
  }

  async #read(window: UsageWindow): Promise<UsageReport> {
    const read = (group: UsageGroup) => this.#api.usageSummary(window, group);
    const [person, feature, model, tool] = await Promise.all([
      read("person"),
      read("feature"),
      read("model"),
      read("tool"),
    ]);
    return { person, feature, model, tool };
  }
}
