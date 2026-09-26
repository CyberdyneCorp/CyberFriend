/**
 * What the agent may reach outside itself, and what it may do when it gets there.
 *
 * The highest-privilege screen in the console. Two rules run through all of
 * it: a tool that changes state looks different from one that does not,
 * everywhere it appears, and enabling one requires typing its name. The
 * second is enforced again by the API; this is the gate the operator meets,
 * not the only one.
 *
 * Split in three to stay readable: `ServersVM` (the servers and adding one),
 * `AllowlistVM` (what may be called) and `TypedConfirmVM` (the gate), with
 * `FederationVM` deciding which path an "Allow" takes.
 */

import { allowlistKeys } from "../domain/allowlist";
import { detailOf } from "../domain/changed";
import { confirmationMatches, toolMutates } from "../domain/effect";
import type { AllowlistEntry, FederatedTool, FederationServer, ToolAllowed } from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { Action } from "./action.svelte";
import { Resource } from "./resource.svelte";

/** A tool an operator picked from a server's list. */
export interface ToolChoice {
  server: string;
  tool: FederatedTool;
}

type FederationApi = Pick<
  AdminApi,
  "servers" | "addServer" | "removeServer" | "allowlist" | "allow" | "disallow"
>;

/**
 * What allowlisting said. The warning is the API noticing that the effect an
 * operator declared and the one the server advertises disagree; it is the
 * most important sentence on this screen when it appears.
 */
export function allowedNote(result: ToolAllowed, tool: string, confirmed: boolean): string {
  const said = detailOf(result, `${tool} is allowlisted`);
  const warning = result.warning ? ` ${result.warning}` : "";
  return confirmed
    ? `${said}. The escalation is recorded against your name.${warning}`
    : `${said}.${warning}`;
}

/**
 * The gate in front of enabling a tool that changes state: the operator reads
 * the tool's name and types it back. Nothing else opens it, and there is no
 * "enable anyway".
 */
export class TypedConfirmVM {
  readonly choice: ToolChoice;
  typed = $state("");
  readonly matches: boolean;

  constructor(choice: ToolChoice) {
    this.choice = choice;
    this.matches = $derived(confirmationMatches(this.typed, choice.tool.name));
  }

  /** Whether the enable button may be pressed; a busy screen never lifts the name check. */
  canConfirm(busy: boolean): boolean {
    return this.matches && !busy;
  }
}

/** The federated servers, and the form that adds one. */
export class ServersVM {
  readonly servers: Resource<FederationServer[]>;
  name = $state("");
  target = $state("");
  readonly adding = new Action();
  readonly canAdd = $derived(
    !this.adding.busy && this.name.trim() !== "" && this.target.trim() !== "",
  );

  readonly #api: Pick<FederationApi, "servers" | "addServer" | "removeServer">;
  readonly #onChanged: () => void;

  constructor(api: Pick<FederationApi, "servers" | "addServer" | "removeServer">, onChanged: () => void) {
    this.#api = api;
    this.#onChanged = onChanged;
    this.servers = new Resource(() => api.servers());
  }

  /**
   * The API probes before accepting and refuses a server it could not reach,
   * so there is no "added but unreachable" case: a failure arrives as a
   * refusal, with the probe's reason in it.
   */
  add = async (): Promise<void> => {
    if (!this.canAdd) return;
    const name = this.name.trim();
    const target = this.target.trim();
    await this.adding.run(async () => {
      const added = await this.#api.addServer(name, target);
      this.name = "";
      this.target = "";
      this.#onChanged();
      return `${detailOf(added, `${added.server.name} added`)}. Nothing it offers is enabled yet.`;
    });
  };

  remove = async (server: FederationServer): Promise<string> => {
    const result = await this.#api.removeServer(server.name);
    this.#onChanged();
    return detailOf(result, `${server.name} removed. Its tools are no longer offered.`);
  };
}

/** What the agent may actually call. */
export class AllowlistVM {
  readonly allowlist: Resource<AllowlistEntry[]>;
  /** `server/tool` keys on the allowlist, for a server list that sent no flag. */
  readonly keys: ReadonlySet<string>;

  readonly #api: Pick<FederationApi, "allowlist" | "allow" | "disallow">;
  readonly #onChanged: () => void;

  constructor(api: Pick<FederationApi, "allowlist" | "allow" | "disallow">, onChanged: () => void) {
    this.#api = api;
    this.#onChanged = onChanged;
    this.allowlist = new Resource(() => api.allowlist());
    this.keys = $derived(allowlistKeys(this.allowlist.data));
  }

  /**
   * `confirm_tool_name` is sent only when the operator typed the name. An
   * enable that reaches the API without it is refused there, which is what
   * makes the gate a gate rather than a decoration.
   */
  allow = async (choice: ToolChoice, confirmed: boolean): Promise<string> => {
    const name = choice.tool.name;
    const result = await this.#api.allow({
      server: choice.server,
      tool: name,
      read_only: !toolMutates(choice.tool),
      ...(confirmed ? { confirm_tool_name: name } : {}),
    });
    this.#onChanged();
    return allowedNote(result, name, confirmed);
  };

  revoke = async (entry: AllowlistEntry): Promise<string> => {
    const result = await this.#api.disallow(entry.server, entry.tool);
    this.#onChanged();
    return detailOf(result, `${entry.tool} revoked.`);
  };
}

export class FederationVM {
  readonly servers: ServersVM;
  readonly allowlist: AllowlistVM;
  /** Allowing, revoking and removing; the outcome shows under the servers' form. */
  readonly action = new Action();
  /** The typed gate, while a state-changing tool is waiting for its name. */
  pending: TypedConfirmVM | null = $state(null);

  constructor(api: FederationApi) {
    this.servers = new ServersVM(api, this.refresh);
    this.allowlist = new AllowlistVM(api, this.refresh);
  }

  load = async (): Promise<void> => {
    await Promise.all([this.servers.servers.reload(), this.allowlist.allowlist.reload()]);
  };

  refresh = (): void => {
    void this.load();
  };

  /**
   * An operator pressed "Allow". Undeclared and undetermined both count as
   * state-changing, so this is the branch a server that declares nothing takes.
   */
  choose = async (choice: ToolChoice): Promise<void> => {
    this.action.clear();
    if (toolMutates(choice.tool)) {
      this.pending = new TypedConfirmVM(choice);
      return;
    }
    await this.#allow(choice, false);
  };

  /** The gate's button. Refuses to send anything unless the name was typed. */
  confirm = async (): Promise<void> => {
    const gate = this.pending;
    if (gate === null || !gate.canConfirm(this.action.busy)) return;
    await this.#allow(gate.choice, true);
  };

  cancel = (): void => {
    this.pending = null;
  };

  removeServer = (server: FederationServer): Promise<void> =>
    this.action.run(() => this.servers.remove(server));

  revoke = (entry: AllowlistEntry): Promise<void> =>
    this.action.run(() => this.allowlist.revoke(entry));

  async #allow(choice: ToolChoice, confirmed: boolean): Promise<void> {
    await this.action.run(async () => {
      const said = await this.allowlist.allow(choice, confirmed);
      this.pending = null;
      return said;
    });
  }
}
