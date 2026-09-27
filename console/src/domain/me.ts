/**
 * The user area's shapes, and the sentences built from them.
 *
 * Only the user area imports this file, as only the console imports
 * `types.ts`: `architecture.test.ts` keeps the two apart.
 */

export interface MeSession {
  email: string | null;
  linked: boolean;
  /** The Discord account the link is to, as "Name (Discord user 123)"; null when not linked. */
  discord: string | null;
  /** Signed in within the last five minutes: "delete everything" may run. */
  fresh: boolean;
}

export interface HeldFact {
  kind: string;
  value: string;
}

export interface Retention {
  tracing: boolean;
  trace_retention_days: number;
  memory_retention_days: number;
  /** null: no database backups are kept. */
  backup_retention_days: number | null;
}

export interface MePrivacy {
  known: boolean;
  archiving: boolean;
  facts: HeldFact[];
  memory: Record<string, number>;
  recent_questions: string[];
  tasks: { id: number; question: string; interval_hours: number; disabled: boolean }[];
  alerts: { id: number; kind: string; chain: string | null; asset: string | null }[];
  voice_seconds_this_month: number;
  suggestions: { id: number; text: string; status: string }[];
  tokens: { label: string | null; issued_at: string }[];
  traces: number;
  retention: Retention;
  identity_account: string;
}

export interface MySuggestion {
  id: number;
  text: string;
  status: string;
  created_at: string;
}

export interface Submitted {
  outcome: string;
  id: number | null;
  message: string;
}

export type ErasureMode = "erase" | "erase_and_opt_out";

export interface Erased {
  mode: ErasureMode;
  counts: Record<string, number>;
  identity_account: string;
}

/** The word typed to confirm "delete everything", as in Discord. */
export const CONFIRM_WORD = "DELETE";

function days(count: number): string {
  return count === 1 ? "1 day" : `${count} days`;
}

/** What the deployment keeps and for how long, as sentences. Never a period it does not keep. */
export function retentionLines(retention: Retention): string[] {
  return [
    retention.tracing
      ? `Questions and answers are traced for quality and kept for ${days(retention.trace_retention_days)}.`
      : "Questions and answers are not traced.",
    `What other people ask me to remember is kept for ${days(retention.memory_retention_days)}.`,
    retention.backup_retention_days === null
      ? "No database backups are kept."
      : `Database backups are kept for ${days(retention.backup_retention_days)}, so a deletion reaches them within that time.`,
  ];
}

/** "A fact kind" from `preferred_name`. */
export function kindLabel(kind: string): string {
  const words = kind.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** `#/me` and anything under it is the user area; everything else is the console. */
export function isUserArea(hash: string): boolean {
  return hash === "/me" || hash.startsWith("/me/");
}
