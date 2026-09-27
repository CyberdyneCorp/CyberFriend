/**
 * The admin API contract, as types.
 *
 * Two things are deliberately absent and their absence is the contract:
 * nothing here carries message, document or ask text, and nothing here
 * carries a secret -- not even a masked one. A feature request's text is not
 * an exception: it is the person's own suggestion, given to the team on
 * purpose, and never something they said in a channel. If a field for either ever
 * appears in this file, the console has started rendering something the API
 * promised never to send.
 *
 * The single exception is `UsageQuestion.question`: the words a person asked
 * the assistant, which the API returns only to an admin signed in with
 * CyberdyneAuth, never with the answer or anything it quoted.
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

/**
 * GET /api/session: who the credential names, and with which roles.
 *
 * `via` says how they got here: a CyberdyneAuth session (`oidc`, the cookie)
 * or an operator token typed into the break-glass form (`token`). The roles
 * are the server's decision; the console only reads them to choose what to
 * show.
 */
export interface Principal {
  subject: string;
  display: string;
  roles: string[];
  via: "oidc" | "token";
}

/** GET /auth/config: whether "Sign in with CyberdyneAuth" is offered at all. */
export interface SignInConfig {
  sign_in: boolean;
}

/** POST /auth/logout: where to send the browser to end the provider's session too. */
export interface SignedOut {
  end_session_url: string | null;
}

/** Where the team is with a suggestion. The API's `RequestStatus`. */
export type FeatureRequestStatus = "new" | "triaged" | "planned" | "done" | "declined" | "duplicate";

/**
 * GET /api/feature-requests: one suggestion as the team sees it.
 *
 * `person` is their display name, never an account id; the console is not
 * where anybody is contacted from. `same_text_elsewhere` counts the other
 * people who suggested the same words.
 */
export interface FeatureRequest {
  id: number;
  text: string;
  language: string | null;
  status: FeatureRequestStatus;
  admin_note: string | null;
  duplicate_of: number | null;
  source_kind: string;
  person: string;
  same_text_elsewhere: number;
  created_at: string;
  updated_at: string;
  updated_by: string | null;
}

export interface FeatureRequestPage {
  items: FeatureRequest[];
  total: number;
  page: number;
  page_size: number;
}

/** PATCH /api/feature-requests/{id}: only the fields that change. */
export interface FeatureRequestPatch {
  status?: FeatureRequestStatus;
  admin_note?: string | null;
  duplicate_of?: number | null;
}

export interface FeatureRequestTriaged extends Changed {
  request: FeatureRequest;
}

/** How GET /api/usage/summary groups its rows. */
export type UsageGroup = "person" | "feature" | "model" | "tool";

/** Inclusive whole UTC days, as `YYYY-MM-DD`. */
export interface UsageWindow {
  from: string;
  to: string;
}

/**
 * One row of the usage summary. A figure that does not apply to the grouping
 * is null (a model has no tool calls), which is not the same as zero.
 */
export interface UsageRow {
  /** A platform user id, a feature, a model or a tool name. */
  key: string;
  /** A person's current display name; null when none is known, or not a person. */
  name: string | null;
  questions: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  /** USD, an estimate from the trace store's price table. */
  cost: number | null;
  tool_calls: number | null;
  tools: string[];
  voice_seconds: number | null;
}

export interface UsageTotals {
  questions: number;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  tool_calls: number;
  voice_seconds: number;
}

/** GET /api/usage/summary. Counts only, never text. */
export interface UsageSummary extends UsageWindow {
  group: UsageGroup;
  /** "traced question runs": the totals undercount by design. */
  label: string;
  /** How long a trace is kept; null when the API could not read the setting. */
  retention_days: number | null;
  rows: UsageRow[];
  totals: UsageTotals;
}

/** One question a person asked: their own words and metadata, nothing of the answer. */
export interface UsageQuestion {
  timestamp: string;
  feature: string;
  tools: string[];
  question: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cost: number | null;
}

/** GET /api/usage/people/{id}/questions: admin signed in with CyberdyneAuth only. */
export interface PersonQuestions extends UsageWindow {
  person: { platform_user_id: string; person_id: number | null; name: string | null };
  page: number;
  total_pages: number;
  /** Questions traced before the person was told they are recorded: counted, never shown. */
  hidden_before_notice: number;
  questions: UsageQuestion[];
}
