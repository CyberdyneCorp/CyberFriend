/**
 * Every setting with its provenance, and an editor per row where one is allowed.
 *
 * The provenance is not decoration. A setting saved here but overridden by an
 * environment variable looks exactly like one that did not save, and the
 * operator's next move -- edit it again, or go and change the deployment --
 * differs completely between the two. So the note after a save is chosen from
 * the provenance the API sends back, not from the fact that it answered.
 */

import { settingText, sourceOf } from "../domain/settings";
import type { Setting } from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { Action } from "./action.svelte";
import { Resource } from "./resource.svelte";

export const SAVED = "Saved.";
export const STILL_OVERRIDDEN =
  "Stored, but an environment variable still overrides it. It will not take effect until that variable is removed from the deployment.";

/** What a save says, from the provenance the API reported after storing it. */
export function savedNote(saved: Pick<Setting, "source">): string {
  return sourceOf(saved.source) === "environment" ? STILL_OVERRIDDEN : SAVED;
}

type PutSetting = AdminApi["putSetting"];

/** One row's edit box: the text being typed, and what the save said. */
export class SettingEditor {
  /** Null while the row shows its value; the operator's text while editing. */
  typed: string | null = $state(null);
  readonly action = new Action();

  readonly #key: string;
  readonly #put: PutSetting;
  readonly #onSaved: () => void;

  constructor(key: string, put: PutSetting, onSaved: () => void) {
    this.#key = key;
    this.#put = put;
    this.#onSaved = onSaved;
  }

  /** Start from the value as the operator types it, not as it is displayed. */
  edit = (setting: Pick<Setting, "value" | "text">): void => {
    this.typed = settingText(setting);
  };

  cancel = (): void => {
    this.typed = null;
  };

  /** Sends the text verbatim: the API parses it and records what was typed. */
  save = async (): Promise<void> => {
    const value = this.typed;
    if (value === null) return;
    await this.action.run(async () => {
      const saved = await this.#put(this.#key, value);
      this.typed = null;
      this.#onSaved();
      return savedNote(saved);
    });
  };
}

/** The settings list, and one editor per key that lives as long as this does. */
export class SettingsVM {
  readonly settings: Resource<Setting[]>;

  readonly #put: PutSetting;
  // Deliberately not reactive: `editor()` is called while a row renders, and
  // writing a reactive map there is an unsafe mutation. The editors hold the
  // reactive state themselves.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  readonly #editors = new Map<string, SettingEditor>();

  constructor(api: Pick<AdminApi, "settings" | "putSetting">) {
    this.settings = new Resource(() => api.settings());
    this.#put = (key, value) => api.putSetting(key, value);
  }

  load = (): Promise<void> => this.settings.reload();

  /** The same editor for a key across reloads, so a reload does not drop a note. */
  editor = (key: string): SettingEditor => {
    let editor = this.#editors.get(key);
    if (editor === undefined) {
      editor = new SettingEditor(key, this.#put, () => void this.load());
      this.#editors.set(key, editor);
    }
    return editor;
  };
}
