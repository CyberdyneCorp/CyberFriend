import { describe, expect, it } from "vitest";

import type { AllowlistEntry, FederatedTool, FederationServer, ToolAllowed } from "../domain/types";
import { FederationVM, TypedConfirmVM, allowedNote } from "./federation.svelte";

const SEARCH: FederatedTool = { name: "search", effect: "read_only", mutates: false };
const RESTART: FederatedTool = { name: "restart_service", effect: "mutating", mutates: true };
const UNDECLARED: FederatedTool = { name: "mystery" };

const SERVERS: FederationServer[] = [
  { name: "ops", target: "https://ops", reachable: true, tools: [SEARCH, RESTART, UNDECLARED] },
];

function fakeApi(warning: string | null = null) {
  const sent: unknown[] = [];
  const calls: string[] = [];
  let allowlist: AllowlistEntry[] = [];
  let reads = 0;
  return {
    sent,
    calls,
    reads: () => reads,
    servers: async () => {
      reads += 1;
      return SERVERS;
    },
    addServer: async (name: string, target: string) => {
      calls.push(`add ${name} ${target}`);
      return { changed: "federation_servers", server: { name, target, reachable: true, tools: [] } };
    },
    removeServer: async (name: string) => {
      calls.push(`remove ${name}`);
      return { changed: "federation_servers" };
    },
    allowlist: async () => allowlist,
    allow: async (entry: {
      server: string;
      tool: string;
      read_only: boolean;
      confirm_tool_name?: string;
    }): Promise<ToolAllowed> => {
      sent.push(entry);
      const added = { server: entry.server, tool: entry.tool, mutation_enabled: !entry.read_only };
      allowlist = [...allowlist, added];
      return { changed: "federation_tool_allowlist", tool: added, warning };
    },
    disallow: async (server: string, tool: string) => {
      calls.push(`disallow ${server}/${tool}`);
      return { changed: "federation_tool_allowlist" };
    },
  };
}

describe("TypedConfirmVM", () => {
  it("opens only for the tool's exact name, and never while busy", () => {
    const gate = new TypedConfirmVM({ server: "ops", tool: RESTART });
    for (const near of ["", "restart", "Restart_Service", "restart_service_2"]) {
      gate.typed = near;
      expect(gate.canConfirm(false)).toBe(false);
    }
    gate.typed = " restart_service ";
    expect(gate.canConfirm(false)).toBe(true);
    expect(gate.canConfirm(true)).toBe(false);
  });
});

describe("allowedNote", () => {
  it("records an escalation when confirmed and repeats the API's warning", () => {
    const result = { changed: "x", tool: { server: "s", tool: "t", mutation_enabled: true }, warning: "declared read-only" };
    expect(allowedNote(result, "t", true)).toBe(
      "t is allowlisted. The escalation is recorded against your name. declared read-only",
    );
    expect(allowedNote({ ...result, warning: null }, "t", false)).toBe("t is allowlisted.");
  });
});

describe("FederationVM", () => {
  it("allowlists a read-only tool at once, read-only and without a confirmation", async () => {
    const api = fakeApi();
    const vm = new FederationVM(api);
    await vm.choose({ server: "ops", tool: SEARCH });
    expect(api.sent).toEqual([{ server: "ops", tool: "search", read_only: true }]);
    expect(vm.pending).toBeNull();
    expect(vm.action.note).toBe("search is allowlisted.");
  });

  it("holds a state-changing tool at the gate until its name is typed", async () => {
    const api = fakeApi();
    const vm = new FederationVM(api);
    await vm.choose({ server: "ops", tool: RESTART });
    expect(api.sent).toEqual([]);
    expect(vm.pending?.choice.tool.name).toBe("restart_service");

    await vm.confirm();
    expect(api.sent).toEqual([]);

    vm.pending!.typed = "restart_service";
    await vm.confirm();
    expect(api.sent).toEqual([
      { server: "ops", tool: "restart_service", read_only: false, confirm_tool_name: "restart_service" },
    ]);
    expect(vm.pending).toBeNull();
    expect(vm.action.note).toContain("escalation is recorded");
  });

  it("treats a tool that declares nothing as state-changing", async () => {
    const api = fakeApi();
    const vm = new FederationVM(api);
    await vm.choose({ server: "ops", tool: UNDECLARED });
    expect(vm.pending).not.toBeNull();
    expect(api.sent).toEqual([]);
  });

  it("forgets the gate on cancel", async () => {
    const vm = new FederationVM(fakeApi());
    await vm.choose({ server: "ops", tool: RESTART });
    vm.cancel();
    expect(vm.pending).toBeNull();
    await vm.confirm();
  });

  it("knows what is allowlisted once the allowlist is reloaded", async () => {
    const api = fakeApi();
    const vm = new FederationVM(api);
    await vm.load();
    await vm.choose({ server: "ops", tool: SEARCH });
    await vm.load();
    expect(vm.allowlist.keys.has("ops/search")).toBe(true);
  });

  it("adds a server from trimmed fields and says nothing it offers is enabled", async () => {
    const api = fakeApi();
    const vm = new FederationVM(api);
    vm.servers.name = " wiki ";
    vm.servers.target = " https://wiki ";
    await vm.servers.add();
    expect(api.calls).toEqual(["add wiki https://wiki"]);
    expect(vm.servers.name).toBe("");
    expect(vm.servers.adding.note).toBe("wiki added. Nothing it offers is enabled yet.");
  });

  it("removes a server and revokes a tool, then reads both lists again", async () => {
    const api = fakeApi();
    const vm = new FederationVM(api);
    await vm.removeServer(SERVERS[0]!);
    await vm.revoke({ server: "ops", tool: "search", mutation_enabled: false });
    expect(api.calls).toEqual(["remove ops", "disallow ops/search"]);
    expect(vm.action.note).toBe("search revoked.");
    expect(api.reads()).toBe(2);
  });
});
