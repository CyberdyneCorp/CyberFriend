/**
 * How long anything is kept, and who has asked to be left out of it.
 *
 * The two belong on one screen because they are the same question asked from
 * two directions: retention is what the archive forgets on a clock, an
 * opt-out is what it never records in the first place.
 */

import { detailOf } from "../domain/changed";
import { parsePerson, type Person } from "../domain/person";
import { isRetentionSetting } from "../domain/settings";
import type { OptOut, Setting } from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { Action } from "./action.svelte";
import { Resource } from "./resource.svelte";
import { SettingsVM } from "./settings.svelte";

export const OPTED_OUT = "Opted out. Nothing further from them is recorded.";

/**
 * An opt-out row, and the person it names when that can be read. `person`
 * arrives as `platform:id`; the endpoint that reverses an opt-out takes the
 * two separately. A row this cannot read gets no button and a reason, never a
 * guessed request -- a guessed id opts the wrong person in.
 */
export interface OptOutRow {
  entry: OptOut;
  person: Person | null;
}

type RetentionApi = Pick<
  AdminApi,
  "settings" | "putSetting" | "optOuts" | "addOptOut" | "removeOptOut"
>;

export class RetentionVM {
  readonly settings: SettingsVM;
  readonly retention: Setting[];

  readonly optOuts: Resource<OptOut[]>;
  readonly rows: OptOutRow[];
  /** The add form. */
  platform = $state("discord");
  userId = $state("");
  readonly adding = new Action();
  /** Opting somebody back in, whose outcome shows above the table. */
  readonly action = new Action();
  readonly canAdd = $derived(!this.adding.busy && this.userId.trim() !== "");

  readonly #api: RetentionApi;

  constructor(api: RetentionApi) {
    this.#api = api;
    this.settings = new SettingsVM(api);
    this.retention = $derived(
      (this.settings.settings.data ?? []).filter((setting) => isRetentionSetting(setting.key)),
    );
    this.optOuts = new Resource(() => api.optOuts());
    this.rows = $derived(
      (this.optOuts.data ?? []).map((entry) => ({ entry, person: parsePerson(entry.person) })),
    );
  }

  load = async (): Promise<void> => {
    await Promise.all([this.settings.load(), this.optOuts.reload()]);
  };

  add = async (): Promise<void> => {
    if (!this.canAdd) return;
    const platform = this.platform.trim();
    const userId = this.userId.trim();
    await this.adding.run(async () => {
      const result = await this.#api.addOptOut(platform, userId);
      this.userId = "";
      void this.optOuts.reload();
      return detailOf(result, OPTED_OUT);
    });
  };

  optBackIn = async (row: OptOutRow): Promise<void> => {
    const person = row.person;
    if (person === null) return;
    await this.action.run(async () => {
      const result = await this.#api.removeOptOut(person.platform, person.id);
      void this.optOuts.reload();
      return detailOf(result, `${row.entry.person} is recorded again from now on.`);
    });
  };
}
