/**
 * A stand-in admin API, shaped like the real one.
 *
 * It exists so the console can be driven end to end -- sign in, navigate,
 * allowlist a tool -- without a database, a Discord token or a model key. The
 * shapes are copied from `src/chatmemory/admin/handlers/*.py`; where this
 * file and those disagree, those are right and this is stale.
 *
 * It enforces the rules the console depends on, so a test cannot pass by
 * accident: the credential is checked on every request (a bearer token, or
 * the session cookie when no bearer is sent), a cookie write without the
 * console header is refused, an operator's write is refused, and a mutating
 * tool is refused unless the confirmation names it. A person's question text
 * is refused to anybody but an admin signed in through CyberdyneAuth, as
 * `admin_oidc` is on the server.
 */

import { createServer, type Server } from "node:http";

export const TOKEN = "cfa_test_credential";

export const SESSION_COOKIE = "__Host-cf_admin";
export const END_SESSION_URL = "https://auth.test/end-session?id_token_hint=stub";

export interface StubPrincipal {
  subject: string;
  display: string;
  roles: string[];
  via: "oidc" | "token";
}

/** Sessions a test can hand the browser, by cookie value. */
export const SESSIONS: Record<string, StubPrincipal> = {
  "operator-session": { subject: "bo-sub", display: "bo@example.com", roles: ["operator"], via: "oidc" },
  "admin-session": { subject: "ana-sub", display: "ana@example.com", roles: ["admin", "operator"], via: "oidc" },
};

type Json = Record<string, unknown>;

function fixtures(): Json {
  return {
    status: {
      status: "ok",
      database_reachable: true,
      ingestion: {
        messages: 128394,
        windows: 8123,
        newest_message_at: "2026-09-16T09:12:00+00:00",
        channels_pending_rebuild: 1,
      },
      embedding_backlog: 31,
      indexed_channels: 4,
      configuration: { settings_from_database: 2, settings_total: 15 },
    },
    settings: [
      {
        key: "web_tools_enabled",
        value: true,
        text: "true",
        source: "database",
        editable: true,
        summary: "whether the agent may reach the public web",
        updated_by: "ana",
        updated_at: "2026-09-15T10:00:00+00:00",
      },
      {
        key: "window_max_tokens",
        value: 1200,
        text: "1200",
        source: "environment",
        editable: true,
        summary: "tokens per retrieval window",
      },
      {
        key: "retention_days",
        value: 365,
        text: "365",
        source: "default",
        editable: true,
        summary: "days a message is kept before the sweep removes it",
      },
      {
        key: "indexed_channel_ids",
        value: ["100", "200"],
        text: "100 200",
        source: "database",
        editable: false,
        managed_by: "/api/channels",
        summary: "channels in indexing scope",
      },
    ],
    servers: [
      {
        name: "wikipedia",
        target: "https://mcp.example/wiki",
        reachable: true,
        failure: null,
        tools: [
          {
            name: "search",
            effect: "read_only",
            mutates: false,
            claims_read_only: true,
            allowlisted: false,
            mutation_enabled: false,
          },
        ],
      },
      {
        name: "ops",
        target: "https://mcp.example/ops",
        reachable: true,
        failure: null,
        tools: [
          {
            name: "restart_service",
            effect: "mutating",
            mutates: true,
            claims_read_only: false,
            allowlisted: false,
            mutation_enabled: false,
          },
          // Declares read-only, and the API determined otherwise. The badge
          // must follow the API, not the claim.
          {
            name: "page_oncall",
            effect: "undetermined",
            mutates: true,
            claims_read_only: true,
            allowlisted: false,
            mutation_enabled: false,
          },
        ],
      },
      {
        name: "stale",
        target: "https://mcp.example/stale",
        reachable: false,
        failure: "probe timed out after 5s",
        tools: [],
      },
    ],
    allowlist: [],
    channels: [
      {
        id: "100",
        name: "general",
        indexed: true,
        readable_by_bot: true,
        messages: 91238,
        last_message_at: "2026-09-16T09:12:00+00:00",
        evidence: null,
      },
      {
        id: "200",
        name: "leadership",
        indexed: true,
        readable_by_bot: false,
        messages: 0,
        last_message_at: null,
        evidence: "the bot has never read a message here",
      },
    ],
    optouts: [{ person: "discord:42", since: "2026-08-01T00:00:00+00:00" }],
    tokens: [
      {
        id: "t1",
        label: "sam's laptop",
        person: "discord:42",
        issued_at: "2026-09-01T09:00:00+00:00",
        revoked_at: null,
      },
    ],
    featureRequests: [
      {
        id: 12,
        text: "Tell me when someone mentions me",
        language: "en",
        status: "new",
        admin_note: null,
        duplicate_of: null,
        source_kind: "command",
        person: "Sam",
        same_text_elsewhere: 2,
        created_at: "2026-09-15T10:00:00+00:00",
        updated_at: "2026-09-15T10:00:00+00:00",
        updated_by: null,
      },
    ],
    usage: usageFixtures(),
    questions: questionFixtures(),
    audit: [
      {
        operator: "ana",
        setting: "discord_token",
        before: null,
        after: null,
        kind: "refused",
        at: "2026-09-16T07:00:00+00:00",
        reason: "credentials are read from the environment and cannot be stored",
      },
    ],
  };
}

const USAGE_TOTALS = {
  questions: 3,
  input_tokens: 1200,
  output_tokens: 120,
  cost: 0.012,
  tool_calls: 2,
  voice_seconds: 150,
};

function usageRow(key: string, name: string | null, extra: Json = {}): Json {
  return {
    key,
    name,
    questions: 1,
    input_tokens: 400,
    output_tokens: 40,
    cost: 0.004,
    tool_calls: 1,
    tools: ["web_search"],
    voice_seconds: null,
    ...extra,
  };
}

/** GET /api/usage/summary's rows per `group`, as `handlers/usage.py` shapes them. */
function usageFixtures(): Json {
  return {
    person: [
      usageRow("1001", "Ana", { questions: 2, voice_seconds: 150 }),
      usageRow("1004", null),
    ],
    feature: [usageRow("corpus.fixed", null), usageRow("voice", null, { questions: null, voice_seconds: 150 })],
    model: [usageRow("gpt-5.4-mini", null, { questions: null, tool_calls: null, tools: [] })],
    tool: [usageRow("web_search", null, { questions: null, input_tokens: null, output_tokens: null, cost: null })],
  };
}

/** GET /api/usage/people/{id}/questions pages, by platform id. */
function questionFixtures(): Record<string, Json[]> {
  const question = (text: string, at: string): Json => ({
    timestamp: at,
    feature: "corpus.fixed",
    tools: ["web_search"],
    question: text,
    input_tokens: 400,
    output_tokens: 40,
    cost: 0.004,
  });
  return {
    "1001": [
      question("what did I miss yesterday?", "2026-09-20T10:00:00+00:00"),
      question("who owns the deploy?", "2026-09-19T10:00:00+00:00"),
    ],
  };
}

/** The usage reads, with the server's rules: 503 when Langfuse is down, `admin_oidc` on text. */
function usageRead(
  state: Json,
  who: StubPrincipal,
  path: string,
  query: URLSearchParams,
  options: StubOptions,
): Reply | null {
  const down = options.usageDown;
  const window = { from: query.get("from") ?? "", to: query.get("to") ?? "" };
  if (path === "/api/usage/summary") {
    if (down) return [503, { error: "usage unavailable" }];
    const group = query.get("group") ?? "person";
    const rows = (state.usage as Record<string, Json[]>)[group] ?? [];
    const retention_days = options.retentionUnknown ? null : 90;
    return [200, { ...window, group, label: "traced question runs", retention_days, rows, totals: USAGE_TOTALS }];
  }
  const match = /^\/api\/usage\/people\/([^/]+)\/questions$/.exec(path);
  if (match === null) return null;
  if (!(who.roles.includes("admin") && who.via === "oidc")) return [403, { error: "requires admin" }];
  if (down) return [503, { error: "usage unavailable" }];
  const id = decodeURIComponent(match[1] ?? "");
  const all = (state.questions as Record<string, Json[]>)[id] ?? [];
  // One question a page, so paging is exercised with two.
  const page = Number(query.get("page") ?? "1");
  return [
    200,
    {
      ...window,
      person: { platform_user_id: id, person_id: 7, name: id === "1001" ? "Ana" : null },
      page,
      total_pages: all.length,
      hidden_before_notice: 1,
      questions: all.slice(page - 1, page),
    },
  ];
}

/**
 * POST /api/federation/allowlist, with the one rule the console depends on:
 * a mutating tool is refused unless the confirmation names it.
 */
function allowTool(state: Json, body: Json): [number, unknown] {
  if (typeof body.read_only !== "boolean") {
    return [400, { error: "read_only is required and must be true or false" }];
  }
  if (!body.read_only && body.confirm_tool_name !== body.tool) {
    return [400, { error: "the confirmation must name the tool being enabled" }];
  }
  const entry = {
    server: String(body.server),
    tool: String(body.tool),
    effect: body.read_only ? "read_only" : "mutating",
    mutation_enabled: !body.read_only,
  };
  (state.allowlist as unknown[]).push(entry);
  // The server list reports the flag too, as the API does.
  const server = (state.servers as { name: string; tools: Json[] }[]).find((s) => s.name === entry.server);
  const tool = server?.tools.find((t) => t.name === entry.tool);
  if (tool !== undefined) Object.assign(tool, { allowlisted: true, mutation_enabled: entry.mutation_enabled });
  return [
    200,
    {
      changed: "federation_tool_allowlist",
      detail: `${entry.server}/${entry.tool} is now available to the agent`,
      tool: entry,
      warning: null,
    },
  ];
}

type Reply = [status: number, payload: unknown];

/** GET /api/feature-requests, with its status filter, as the API pages it. */
function featurePage(state: Json, url: string): Json {
  const status = new URL(url, "http://stub").searchParams.get("status");
  const items = (state.featureRequests as Json[]).filter((row) => status === null || row.status === status);
  return { items, total: items.length, page: 1, page_size: 50 };
}

/** One write route: the method, the path prefix, and what it does to the state. */
type WriteRoute = [method: string, prefix: string, handle: (state: Json, last: string, body: Json, path: string) => Reply];

const rows = (state: Json, name: string) => state[name] as Json[];

/**
 * The writes the other screens make, in the API's shapes. The stub records
 * each one (`writes`), so a test asserts on the request rather than the screen.
 */
const WRITES: WriteRoute[] = [
  ["POST", "/api/channels", (state, _last, body) => {
    const channel = { id: String(body.id), name: "", indexed: true, readable_by_bot: false, evidence: null };
    rows(state, "channels").push(channel);
    return [200, { changed: "indexed_channel_ids", channel }];
  }],
  ["DELETE", "/api/channels/", (state, last) => {
    state.channels = rows(state, "channels").filter((channel) => channel.id !== last);
    return [200, { changed: "indexed_channel_ids" }];
  }],
  ["PUT", "/api/settings/", (state, last, body) => {
    const setting = rows(state, "settings").find((row) => row.key === last);
    if (setting === undefined) return [404, { error: "no such setting" }];
    setting.text = String(body.value);
    return [200, setting];
  }],
  ["DELETE", "/api/tokens/", (state, last) => {
    const token = rows(state, "tokens").find((row) => row.id === last);
    if (token !== undefined) token.revoked_at = "2026-09-16T10:00:00+00:00";
    return [200, { changed: "mcp_token" }];
  }],
  ["POST", "/api/federation/servers", (state, _last, body) => {
    const server = { name: String(body.name), target: String(body.target), reachable: true, failure: null, tools: [] };
    rows(state, "servers").push(server);
    return [200, { changed: "federation_servers", detail: `${server.name} answered and offers 0 tool(s)`, server }];
  }],
  ["DELETE", "/api/federation/servers/", (state, last) => {
    state.servers = rows(state, "servers").filter((server) => server.name !== last);
    return [200, { changed: "federation_servers", detail: `${last} removed` }];
  }],
  ["DELETE", "/api/federation/allowlist/", (state, last, _body, path) => {
    const [, , , , server] = path.split("/");
    state.allowlist = rows(state, "allowlist").filter((entry) => !(entry.server === server && entry.tool === last));
    return [200, { changed: "federation_tool_allowlist", detail: `${server}/${last} is no longer available` }];
  }],
  ["POST", "/api/optouts", (state, _last, body) => {
    const person = `${String(body.platform)}:${String(body.platform_user_id)}`;
    rows(state, "optouts").push({ person, since: "2026-09-16T10:00:00+00:00" });
    return [200, { changed: `opt_out:${person}`, detail: "the exclusion is enforced in the database", removed: {} }];
  }],
  ["PATCH", "/api/feature-requests/", (state, last, body) => {
    const request = rows(state, "featureRequests").find((row) => String(row.id) === last);
    if (request === undefined) return [404, { error: "no such suggestion" }];
    Object.assign(request, body, { updated_by: "ana" });
    return [200, { changed: `feature_request.${last}`, request }];
  }],
  ["DELETE", "/api/optouts/", (state, _last, _body, path) => {
    const [, , , platform, id] = path.split("/");
    state.optouts = rows(state, "optouts").filter((row) => row.person !== `${platform}:${id}`);
    return [200, { changed: "opt_out" }];
  }],
];

function otherWrites(state: Json, method: string, path: string, body: Json): Reply | null {
  const route = WRITES.find(([verb, prefix]) =>
    verb === method && (prefix.endsWith("/") ? path.startsWith(prefix) : path === prefix),
  );
  if (route === undefined) return null;
  const last = decodeURIComponent(path.split("/").pop() ?? "");
  return route[2](state, last, body, path);
}

type Headers = Record<string, string | string[] | undefined>;

function cookieOf(headers: Headers, name: string): string | null {
  const raw = headers.cookie;
  const text = Array.isArray(raw) ? raw.join(";") : (raw ?? "");
  for (const part of text.split(";")) {
    const [key, value] = part.trim().split("=");
    if (key === name && value) return value;
  }
  return null;
}

/**
 * Who the request is from, as the API decides it: a bearer alone when one is
 * sent (cookies ignored), else the session cookie. With sign-in configured a
 * token is operator only, as on the server.
 */
function principalOf(headers: Headers, sessions: Map<string, StubPrincipal>, signIn: boolean): StubPrincipal | null {
  if (headers.authorization !== undefined) {
    if (headers.authorization !== `Bearer ${TOKEN}`) return null;
    return { subject: "sam", display: "sam", roles: signIn ? ["operator"] : ["admin", "operator"], via: "token" };
  }
  const id = cookieOf(headers, SESSION_COOKIE);
  return id === null ? null : (sessions.get(id) ?? null);
}

/** Why a write by `who` is refused, or null when it is not. */
function writeRefusal(who: StubPrincipal, headers: Headers): Reply | null {
  if (who.via === "oidc" && headers["x-cyberfriend-console"] !== "1") {
    return [403, { error: "cross-site request refused" }];
  }
  return who.roles.includes("admin") ? null : [403, { error: "requires admin" }];
}

/** One request as the stub saw it: what credential it carried. */
/**
 * Switches a test flips: whether `/auth/config` says sign-in is offered,
 * whether Langfuse is down, and whether the API could read
 * `TRACE_RETENTION_DAYS`.
 */
export interface StubOptions {
  signIn: boolean;
  usageDown: boolean;
  retentionUnknown: boolean;
}

export interface Seen {
  method: string;
  path: string;
  /** The query string, without the `?`. */
  query: string;
  authorization: string | null;
  cookie: string | null;
}

export interface StubApi {
  origin: string;
  /** The stub's data, which a test may change before the console reads it. */
  state: Json;
  /** Every allowlist POST the console made, to assert on what it sent. */
  allowCalls: Json[];
  /** Every other write, as `METHOD /path body`, in order. */
  writes: string[];
  /** Every request, with the credential it carried. */
  seen: Seen[];
  /** Live sessions by cookie value; `SESSIONS` to start with. */
  sessions: Map<string, StubPrincipal>;
  options: StubOptions;
  close: () => Promise<void>;
}

export async function startStubApi(): Promise<StubApi> {
  const state = fixtures();
  const allowCalls: Json[] = [];
  const writes: string[] = [];
  const seen: Seen[] = [];
  const sessions = new Map(Object.entries(SESSIONS));
  const options: StubOptions = { signIn: false, usageDown: false, retentionUnknown: false };

  const server: Server = createServer((request, response) => {
    const [path = "", query = ""] = (request.url ?? "").split("?");
    const reply = (status: number, payload: unknown): void => {
      const body = JSON.stringify(payload);
      response.writeHead(status, {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(body),
      });
      response.end(body);
    };

    const method = request.method ?? "";
    const headers = request.headers as Headers;
    seen.push({
      method,
      path,
      query,
      authorization: request.headers.authorization ?? null,
      cookie: request.headers.cookie ?? null,
    });

    if (method === "GET" && path === "/auth/config") {
      reply(200, { sign_in: options.signIn });
      return;
    }
    if (method === "POST" && path === "/auth/logout") {
      if (headers["x-cyberfriend-console"] !== "1") {
        reply(403, { error: "cross-site request refused" });
        return;
      }
      const id = cookieOf(headers, SESSION_COOKIE);
      if (id !== null) sessions.delete(id);
      reply(200, { end_session_url: END_SESSION_URL });
      return;
    }

    const who = principalOf(headers, sessions, options.signIn);
    if (who === null) {
      // One refusal for missing, malformed, unknown and revoked, as the API
      // does it: a refusal that distinguished them is an enumeration oracle.
      reply(401, { error: "unauthorized" });
      return;
    }
    const refused = method === "GET" ? null : writeRefusal(who, headers);
    if (refused !== null) {
      reply(...refused);
      return;
    }

    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const body: Json =
        chunks.length === 0 ? {} : (JSON.parse(Buffer.concat(chunks).toString()) as Json);

      if (request.method === "GET") {
        const usage = usageRead(state, who, path, new URLSearchParams(query), options);
        if (usage !== null) {
          reply(...usage);
          return;
        }
        const reads: Record<string, unknown> = {
          "/api/session": who,
          "/api/status": state.status,
          "/api/settings": state.settings,
          "/api/federation/servers": state.servers,
          "/api/federation/allowlist": state.allowlist,
          "/api/channels": state.channels,
          "/api/optouts": state.optouts,
          "/api/tokens": state.tokens,
          "/api/audit": state.audit,
          "/api/feature-requests": featurePage(state, request.url ?? ""),
        };
        const found = reads[path];
        reply(found === undefined ? 404 : 200, found ?? { error: "no such route" });
        return;
      }

      if (request.method === "POST" && path === "/api/federation/allowlist") {
        allowCalls.push(body);
        const [status, payload] = allowTool(state, body);
        reply(status, payload);
        return;
      }

      writes.push(`${method} ${path}${chunks.length === 0 ? "" : ` ${JSON.stringify(body)}`}`);
      const [status, payload] = otherWrites(state, method, path, body) ?? [404, { error: "no such route" }];
      reply(status, payload);
    });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("no port");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    state,
    allowCalls,
    writes,
    seen,
    sessions,
    options,
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      ),
  };
}
