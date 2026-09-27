/**
 * Reading a setting, and saying where its value came from.
 *
 * The API takes a setting's new value as a **string** and parses it against
 * the setting's own type, so this console sends the operator's text verbatim
 * rather than coercing it to a JSON type. That is not a shortcut: the change
 * record has to hold exactly what was typed, and a console that helpfully
 * turned "180" into `180` would put a different value in the record from the
 * one the operator entered.
 *
 * It also means an unparseable value is refused by the server, with the
 * refusal recorded. A console that guessed would hide the mistake instead.
 */

import type { JsonValue, Setting, SettingSource } from "./types";

export type Provenance = "database" | "environment" | "default" | "unknown";

/**
 * Both spellings of every source. The written contract says `db|env|default`
 * and the running API sends `database|environment|default`; a console that
 * knew only one of them would render the most important column on the screen
 * as a blank or as a lie. Anything else is reported as unknown rather than
 * guessed -- claiming a value came from the database when nobody knows is the
 * failure this column exists to prevent.
 */
export function sourceOf(source: SettingSource | string): Provenance {
  switch (source) {
    case "db":
    case "database":
      return "database";
    case "env":
    case "environment":
      return "environment";
    case "default":
      return "default";
    default:
      return "unknown";
  }
}

/** The value as the operator types it: the API's own rendering when it sent one. */
export function settingText(setting: Pick<Setting, "value" | "text">): string {
  if (typeof setting.text === "string") return setting.text;
  return renderValue(setting.value);
}

function renderValue(value: JsonValue): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map(renderValue).join(" ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Settings an operator thinks of as retention, for the retention screen. */
export function isRetentionSetting(key: string): boolean {
  return key.startsWith("retention") || key.includes("retain");
}
