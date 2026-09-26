// @vitest-environment jsdom
/**
 * The Svelte console, driven end to end against the stub API.
 *
 * The view-model tests pin the rules; this pins that they are wired to the
 * screen: real services, real views, and the `node:http` stub in
 * `test/stubApi.ts`, so it needs no database, Discord token or model key.
 *
 * It is here because of the bug it first caught in the earlier React console: sign-in
 * put the token in the session and checked it afterwards, so a refused
 * credential unmounted the form mid-request and the operator got a blank box
 * and no message. Every piece was individually correct.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { tick } from "svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { adminApi } from "./services/adminApi";
import { browserHash } from "./services/hashLocation";
import { session, signOut } from "./services/session";
import { startStubApi, TOKEN, type StubApi } from "./test/stubApi";
import { ConsoleVM } from "./viewmodels/console.svelte";
import App from "./views/App.svelte";

let api: StubApi;
const realFetch = globalThis.fetch;

beforeEach(async () => {
  // jsdom keeps one window for the whole file, and the router reads the hash
  // from it, so a test would otherwise start where the last one left off.
  window.location.hash = "";
  api = await startStubApi();
  // The console asks for "/api/...", which jsdom resolves against its own origin.
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
    realFetch(new URL(String(input), api.origin), init)) as typeof fetch;
  render(App, { props: { app: new ConsoleVM(adminApi, session, browserHash) } });
});

afterEach(async () => {
  cleanup();
  signOut();
  globalThis.fetch = realFetch;
  await api.close();
});

async function signIn(credential: string): Promise<void> {
  await fireEvent.input(screen.getByPlaceholderText("cfa_…"), { target: { value: credential } });
  await fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("signing in", () => {
  it("says so when the credential is refused, and stays on the form", async () => {
    await signIn("cfa_not_a_real_token");
    await screen.findByText("That credential was refused.");
    expect(screen.getByPlaceholderText("cfa_…")).toBeTruthy();
    expect(screen.queryByText("Ingestion")).toBeNull();
  });

  it("opens the status screen with a credential that works", async () => {
    await signIn(TOKEN);
    await screen.findByText("Ingestion");
    expect(screen.getByText("128,394")).toBeTruthy();
    // The unknown (empty) address was sent to the status screen.
    expect(window.location.hash).toBe("#/status");
  });
});

describe("the status screen", () => {
  beforeEach(async () => {
    await signIn(TOKEN);
    await screen.findByText("Ingestion");
  });

  it("lists the recent configuration changes, refusals included", async () => {
    await screen.findByText("discord_token");
    expect(screen.getByText("refused").className).toContain("badge--kind-refused");
  });

  it("goes back to the status screen from an address that names no screen", async () => {
    window.location.hash = "#/federation";
    await screen.findByText("restart_service");
    window.location.hash = "#/no-such-screen";
    await screen.findByText("Ingestion");
    await waitFor(() => expect(window.location.hash).toBe("#/status"));
  });

  it("forgets the credential and the screens on sign-out", async () => {
    await fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByPlaceholderText("cfa_…");
    expect(screen.queryByText("Ingestion")).toBeNull();
  });
});

/** Open a screen from the navigation, and wait for something only it shows. */
async function open(title: string, shows: string): Promise<void> {
  await signIn(TOKEN);
  await screen.findByText("Ingestion");
  await fireEvent.click(screen.getByRole("link", { name: title }));
  await screen.findAllByText(shows);
}

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

/** `click()`, not `fireEvent.click`: like a browser, it does nothing on a disabled button. */
async function press(target: HTMLButtonElement): Promise<void> {
  target.click();
  await tick();
}

/** The `li.tool` row for a tool on the federation screen. */
function toolRow(name: string): HTMLElement {
  const row = Array.from(document.querySelectorAll<HTMLElement>("li.tool")).find(
    (candidate) => candidate.querySelector(".tool__name")?.textContent === name,
  );
  if (row === undefined) throw new Error(`no tool row for ${name}`);
  return row;
}

/** The card for a federated server. */
function serverCard(name: string): HTMLElement {
  const card = screen.getByRole("heading", { name }).closest("article");
  if (card === null) throw new Error(`no card for ${name}`);
  return card;
}

/** The allowlist table's row for `server/tool`. */
function allowlistRow(server: string, tool: string): HTMLElement {
  const row = Array.from(document.querySelectorAll<HTMLElement>("tbody tr")).find((candidate) => {
    const cells = Array.from(candidate.querySelectorAll("td"), (cell) => cell.textContent?.trim());
    return cells[0] === server && cells[1] === tool;
  });
  if (row === undefined) throw new Error(`no allowlist row for ${server}/${tool}`);
  return row;
}

/** The table row that mentions `text`. */
function rowOf(text: string): HTMLElement {
  const row = screen.getByText(text).closest("tr");
  if (row === null) throw new Error(`no row mentions ${text}`);
  return row;
}

describe("the federation screen", () => {
  beforeEach(() => open("Federation", "restart_service"));

  it("marks a state-changing tool differently from a read-only one", () => {
    expect(toolRow("restart_service").className).toContain("tool--mutating");
    expect(toolRow("search").className).not.toContain("tool--mutating");
    expect(toolRow("restart_service").textContent).toContain("state-changing");
    // A server's own claim does not soften the badge.
    expect(toolRow("page_oncall").textContent).toContain("state-changing (undetermined)");
    expect(toolRow("page_oncall").textContent).toContain("the server claims read-only");
  });

  it("says an unreachable server is unreachable, rather than empty", () => {
    expect(screen.getByText("probe timed out after 5s")).toBeTruthy();
    expect(screen.getByText("No tools discovered; the last probe did not reach it.")).toBeTruthy();
  });

  it("allowlists a read-only tool without a confirmation", async () => {
    await fireEvent.click(within(toolRow("search")).getByRole("button", { name: "Allow" }));
    await screen.findByText(/is now available to the agent/);
    expect(api.allowCalls).toEqual([{ server: "wikipedia", tool: "search", read_only: true }]);
    // The allowlist panel shows it, and the tool row no longer offers "Allow".
    await waitFor(() => expect(within(toolRow("search")).queryByRole("button")).toBeNull());
  });

  it("will not enable a state-changing tool until its name is typed", async () => {
    await fireEvent.click(within(toolRow("restart_service")).getByRole("button", { name: "Allow" }));
    const gate = await screen.findByRole("group", { name: "Enable restart_service" });
    const input = within(gate).getByRole("textbox");

    expect(button("Enable this tool").disabled).toBe(true);
    await press(button("Enable this tool"));
    expect(api.allowCalls).toEqual([]);

    await fireEvent.input(input, { target: { value: "restart" } });
    expect(button("Enable this tool").disabled).toBe(true);

    await fireEvent.input(input, { target: { value: "restart_service" } });
    expect(button("Enable this tool").disabled).toBe(false);
    await press(button("Enable this tool"));
    await screen.findByText(/escalation is recorded/);
    expect(api.allowCalls).toEqual([
      { server: "ops", tool: "restart_service", read_only: false, confirm_tool_name: "restart_service" },
    ]);
  });

  it("adds a server with the name and target typed", async () => {
    await fireEvent.input(screen.getByLabelText("Name"), { target: { value: " issues " } });
    await fireEvent.input(screen.getByLabelText("Target"), { target: { value: "https://mcp.example/issues" } });
    await fireEvent.click(button("Add server"));
    await screen.findByText(/issues answered and offers 0 tool\(s\)\. Nothing it offers is enabled yet\./);
    expect(api.writes).toEqual([
      'POST /api/federation/servers {"name":"issues","target":"https://mcp.example/issues"}',
    ]);
    await screen.findByText("https://mcp.example/issues");
  });

  it("removes a server only on the second click, and only that server", async () => {
    await fireEvent.click(within(serverCard("stale")).getByRole("button", { name: "Remove" }));
    expect(api.writes).toEqual([]);
    await press(within(serverCard("stale")).getByRole("button", { name: "Remove this server" }));
    await screen.findByText("stale removed");
    expect(api.writes).toEqual(["DELETE /api/federation/servers/stale"]);
    await waitFor(() => expect(screen.queryByText("probe timed out after 5s")).toBeNull());
  });

  it("will not remove a server while another change is in flight", async () => {
    await fireEvent.click(within(serverCard("stale")).getByRole("button", { name: "Remove" }));
    // The allow's request is still open when the remove is pressed.
    await fireEvent.click(within(toolRow("search")).getByRole("button", { name: "Allow" }));
    const remove = within(serverCard("stale")).getByRole("button", { name: "Remove this server" });
    expect((remove as HTMLButtonElement).disabled).toBe(true);
    await screen.findByText(/is now available to the agent/);
    expect(api.writes).toEqual([]);
  });

  it("revokes an allowlisted tool only on the second click", async () => {
    await fireEvent.click(within(toolRow("search")).getByRole("button", { name: "Allow" }));
    await screen.findByText(/is now available to the agent/);
    const entry = await waitFor(() => allowlistRow("wikipedia", "search"));
    await fireEvent.click(within(entry).getByRole("button", { name: "Revoke" }));
    expect(api.writes).toEqual([]);
    await press(within(allowlistRow("wikipedia", "search")).getByRole("button", { name: "Revoke this tool" }));
    await screen.findByText("wikipedia/search is no longer available");
    expect(api.writes).toEqual(["DELETE /api/federation/allowlist/wikipedia/search"]);
    await screen.findByText("Nothing is allowlisted.");
  });
});

describe("the channels screen", () => {
  beforeEach(() => open("Channels", "leadership"));

  it("says whether the bot can read each indexed channel", () => {
    expect(within(rowOf("leadership")).getByText("cannot read")).toBeTruthy();
    expect(screen.getByText("the bot has never read a message here")).toBeTruthy();
    expect(within(rowOf("general")).getByText("can read")).toBeTruthy();
  });

  it("says out loud that a channel it just added cannot be read", async () => {
    await fireEvent.input(screen.getByPlaceholderText("e.g. 1234567890"), { target: { value: " 300 " } });
    await fireEvent.click(button("Index this channel"));
    await screen.findByText(/the bot cannot read it, so nothing will be indexed/);
    expect(api.writes).toEqual(['POST /api/channels {"id":"300"}']);
  });

  it("stops indexing a channel only on the second click", async () => {
    await fireEvent.click(within(rowOf("general")).getByRole("button", { name: "Stop indexing" }));
    expect(api.writes).toEqual([]);
    await fireEvent.click(within(rowOf("general")).getByRole("button", { name: "Stop indexing it" }));
    await screen.findByText("general is out of scope.");
    expect(api.writes).toEqual(["DELETE /api/channels/100"]);
  });
});

describe("the settings screen", () => {
  beforeEach(() => open("Settings", "web_tools_enabled"));

  it("says where every setting's value came from", () => {
    const badges = Array.from(document.querySelectorAll(".badge"))
      .map((badge) => badge.textContent?.trim())
      .filter((text) => text === "database" || text === "environment" || text === "default");
    // One per row, not one for the table: a row without provenance is a row
    // an operator cannot act on.
    expect(badges.length).toBe(document.querySelectorAll("tbody tr").length);
    expect(screen.getByText("managed at /api/channels")).toBeTruthy();
  });

  it("says a saved value the environment still overrides is not in force", async () => {
    await fireEvent.click(within(rowOf("window_max_tokens")).getByRole("button", { name: "Edit" }));
    const input = within(rowOf("window_max_tokens")).getByRole("textbox");
    expect((input as HTMLInputElement).value).toBe("1200");
    await fireEvent.input(input, { target: { value: "1500" } });
    await fireEvent.click(within(rowOf("window_max_tokens")).getByRole("button", { name: "Save" }));
    await screen.findByText(/an environment variable still overrides it/);
    expect(api.writes).toEqual(['PUT /api/settings/window_max_tokens {"value":"1500"}']);
  });
});

describe("the retention screen without a retention setting", () => {
  it("says there is none rather than showing an editor that would not save", async () => {
    api.state.settings = (api.state.settings as { key: string }[]).filter((row) => row.key !== "retention_days");
    await open("Retention", "discord:42");
    expect(screen.getByText(/The API exposes no retention setting/)).toBeTruthy();
  });
});

describe("the retention screen", () => {
  beforeEach(() => open("Retention", "discord:42"));

  it("shows only the retention settings, and saves one under its own key", async () => {
    expect(screen.queryByText("window_max_tokens")).toBeNull();
    await fireEvent.click(within(rowOf("retention_days")).getByRole("button", { name: "Edit" }));
    await fireEvent.input(within(rowOf("retention_days")).getByRole("textbox"), { target: { value: "90" } });
    await fireEvent.click(within(rowOf("retention_days")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.writes).toEqual(['PUT /api/settings/retention_days {"value":"90"}']));
  });

  it("opts a person out by platform and id", async () => {
    await fireEvent.input(screen.getByLabelText("Platform user id"), { target: { value: " 77 " } });
    await fireEvent.click(button("Opt this person out"));
    await screen.findByText("the exclusion is enforced in the database");
    expect(api.writes).toEqual(['POST /api/optouts {"platform":"discord","platform_user_id":"77"}']);
    await screen.findByText("discord:77");
  });

  it("opts a person back in only on the second click, by platform and id", async () => {
    await fireEvent.click(button("Opt back in"));
    expect(api.writes).toEqual([]);
    await fireEvent.click(button("Record them again"));
    await screen.findByText("discord:42 is recorded again from now on.");
    expect(api.writes).toEqual(["DELETE /api/optouts/discord/42"]);
  });
});

describe("the tokens screen", () => {
  beforeEach(() => open("Tokens", "sam's laptop"));

  it("offers no way to issue a token", () => {
    expect(screen.queryByRole("button", { name: /issue|mint|new/i })).toBeNull();
    expect(screen.getByText("python -m chatmemory.mcp.issue_token")).toBeTruthy();
  });

  it("revokes a token only on the second click", async () => {
    await fireEvent.click(button("Revoke"));
    expect(api.writes).toEqual([]);
    await fireEvent.click(button("Revoke this token"));
    await screen.findByText("Revoked. It stops working immediately.");
    expect(api.writes).toEqual(["DELETE /api/tokens/t1"]);
    await screen.findByText(/^revoked /);
  });
});

describe("the audit screen", () => {
  // The reason is shown only here; the setting's name is on the status screen too.
  beforeEach(() => open("Audit", "credentials are read from the environment and cannot be stored"));

  it("shows the record of what has been refused", () => {
    expect(screen.getByText("credentials are read from the environment and cannot be stored")).toBeTruthy();
    expect(within(rowOf("discord_token")).getByText("refused").className).toContain("kind-refused");
  });

  it("filters by kind, and says when a kind has nothing", async () => {
    await fireEvent.click(button("escalation"));
    await screen.findByText("No escalation changes.");
    await fireEvent.click(button("refused"));
    await screen.findByText("discord_token");
  });
});
