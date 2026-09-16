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

export interface StubApi {
  origin: string;
  /** Every allowlist POST the console made, to assert on what it sent. */
  allowCalls: Json[];
  close: () => Promise<void>;
}

export async function startStubApi(): Promise<StubApi> {
  const state = fixtures();
  const allowCalls: Json[] = [];

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
        if (typeof body.read_only !== "boolean") {
          reply(400, { error: "read_only is required and must be true or false" });
          return;
        }
        if (!body.read_only && body.confirm_tool_name !== body.tool) {
          reply(400, { error: "the confirmation must name the tool being enabled" });
          return;
        }
        const entry = {
          server: String(body.server),
          tool: String(body.tool),
          effect: body.read_only ? "read_only" : "mutating",
          mutation_enabled: !body.read_only,
        };
        (state.allowlist as unknown[]).push(entry);
        reply(200, {
          changed: "federation_tool_allowlist",
          detail: `${entry.server}/${entry.tool} is now available to the agent`,
          tool: entry,
          warning: null,
        });
        return;
      }

      reply(404, { error: "no such route" });
    });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("no port");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    allowCalls,
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      ),
  };
}
