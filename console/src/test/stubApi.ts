/**
 * A stand-in admin API, shaped like the real one.
 *
 * It exists so the console can be driven end to end -- sign in, navigate,
 * allowlist a tool -- without a database, a Discord token or a model key. The
 * shapes are copied from `src/chatmemory/admin/handlers/*.py`; where this
 * file and those disagree, those are right and this is stale.
 *
 * It enforces the two rules the console depends on, so a test cannot pass by
 * accident: the credential is checked on every request, and a mutating tool
 * is refused unless the confirmation names it.
 */

import { createServer, type Server } from "node:http";

export const TOKEN = "cfa_test_credential";

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

export interface StubApi {
  origin: string;
  /** Every allowlist POST the console made, to assert on what it sent. */
  allowCalls: Json[];
  /** Every other write, as `METHOD /path body`, in order. */
  writes: string[];
  close: () => Promise<void>;
}

export async function startStubApi(): Promise<StubApi> {
  const state = fixtures();
  const allowCalls: Json[] = [];
  const writes: string[] = [];

  const server: Server = createServer((request, response) => {
    const path = (request.url ?? "").split("?")[0] ?? "";
    const reply = (status: number, payload: unknown): void => {
      const body = JSON.stringify(payload);
      response.writeHead(status, {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(body),
      });
      response.end(body);
    };

    if (request.headers.authorization !== `Bearer ${TOKEN}`) {
      // One refusal for missing, malformed, unknown and revoked, as the API
      // does it: a refusal that distinguished them is an enumeration oracle.
      reply(401, { error: "unauthorized" });
      return;
    }

    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const body: Json =
        chunks.length === 0 ? {} : (JSON.parse(Buffer.concat(chunks).toString()) as Json);

      if (request.method === "GET") {
        const reads: Record<string, unknown> = {
          "/api/status": state.status,
          "/api/settings": state.settings,
          "/api/federation/servers": state.servers,
          "/api/federation/allowlist": state.allowlist,
          "/api/channels": state.channels,
          "/api/optouts": state.optouts,
          "/api/tokens": state.tokens,
          "/api/audit": state.audit,
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

      const method = request.method ?? "";
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
    allowCalls,
    writes,
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      ),
  };
}
