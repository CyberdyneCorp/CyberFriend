/**
 * The admin API contract, as types.
 *
 * Two things are deliberately absent and their absence is the contract:
 * nothing here carries message, document or ask text, and nothing here
 * carries a secret -- not even a masked one. If a field for either ever
 * appears in this file, the console has started rendering something the API
 * promised never to send.
 *
 * Nothing here names an operator either. The credential decides who is
 * acting; a request that could name one would make the audit trail an
 * assertion rather than a fact.
 *
 * Optional fields are optional because the API adds detail this console can
 * use but does not require. A field marked `?` here is one the screens render
 * when it arrives and do without when it does not -- never one whose absence
 * is quietly read as a safe default. The single place that rule would matter,
 * a tool's effect, resolves an absent field to *state-changing*.
 */

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/**
 * GET /api/status.
 *
 * Read as a bag of named groups rather than a fixed shape. The contract fixes
 * the categories -- health, ingestion progress, embedding backlog -- and not
 * the field names inside them, and a console that hard-codes guessed names
 * shows a blank card exactly where the interesting number is. The status
 * screen renders whatever scalars arrive, so a field the API adds appears
 * without a console release.
 */
export type Status = Record<string, JsonValue>;

/**
 * What a successful change comes back as: what changed, and a sentence about
 * what it means. The sentence is written by the handler that made the change
 * -- "ingestion picks it up at the next configuration refresh" -- so the
 * console renders it rather than inventing its own, which would drift from
 * what the system actually did.
 */
export interface Changed {
  changed: string;
  detail?: string;
}

/** Where a value in force came from. Both spellings; see `sourceOf`. */
export type SettingSource =
  | "database"
  | "environment"
  | "default"
  | "db"
  | "env";

export interface Setting {
  key: string;
  value: JsonValue;
  /** The value as an operator types it and as the store holds it. */
  text?: string;
  source: SettingSource;
  editable: boolean;
  /** For a setting edited on another screen: which endpoint owns it. */
  managed_by?: string | null;
  summary?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
}

export interface FederatedTool {
  name: string;
  /** `read_only`, `mutating` or `undetermined`. */
  effect?: string | null;
  /** The API's own verdict. Absent is not "no"; see `domain/effect.ts`. */
  mutates?: boolean;
  /** The server's claim, which grants nothing by itself. */
  claims_read_only?: boolean;
  allowlisted?: boolean;
  mutation_enabled?: boolean;
}

export interface FederationServer {
  name: string;
  target: string;
  reachable: boolean;
  /** Why the probe did not reach it. */
  failure?: string | null;
  tools: FederatedTool[];
}

export interface AllowlistEntry {
  server: string;
  tool: string;
  effect?: string | null;
  mutation_enabled: boolean;
  /** Whose authority a call carries. */
  credential?: string | null;
}

export interface ServerAdded extends Changed {
  server: FederationServer;
}

export interface ToolAllowed extends Changed {
  tool: AllowlistEntry;
  /** Set when the operator's declaration and the server's disagree. */
  warning?: string | null;
}

export interface Channel {
  id: string;
  name: string;
  indexed: boolean;
  readable_by_bot: boolean;
  messages?: number;
  last_message_at?: string | null;
  /** Why the console believes the bot cannot read it. */
  evidence?: string | null;
}

export interface ChannelAdded extends Changed {
  channel: Channel;
}

/** `person` is `platform:id`; see `domain/person.ts` for why that matters. */
export interface OptOut {
  person: string;
  since: string;
}

export interface TokenRecord {
  /** What DELETE /api/tokens/{id} takes. Absent rows cannot be revoked here. */
  id?: string | null;
  label: string;
  person: string;
  issued_at: string;
  revoked_at?: string | null;
}

export type ChangeKind = "applied" | "refused" | "escalation";

export interface AuditEntry {
  operator: string;
  setting: string;
  before: string | null;
  after: string | null;
  kind: ChangeKind;
  at: string;
  reason?: string | null;
}
