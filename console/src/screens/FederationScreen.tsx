/**
 * What the agent may reach outside itself, and what it may do when it gets there.
 *
 * The highest-privilege screen in the console. Two rules run through all of
 * it: a tool that changes state looks different from one that does not,
 * everywhere it appears, and enabling one requires typing its name. The
 * second is enforced again by the API; this is the gate the operator meets,
 * not the only one.
 */

import { useState } from "react";

import { api, detailOf } from "../api/client";
import type { AllowlistEntry } from "../api/types";
import { ActionResult } from "../components/ActionResult";
import { ToolEffectBadge, YesNoBadge } from "../components/Badges";
import { ConfirmButton } from "../components/ConfirmButton";
import { Loaded, Panel } from "../components/DataState";
import { ServerCard, type ToolChoice } from "../components/ServerCard";
import { TypedConfirm } from "../components/TypedConfirm";
import { toolMutates } from "../domain/effect";
import { useAction } from "../hooks/useAction";
import { useResource } from "../hooks/useResource";

function allowlistKeys(entries: AllowlistEntry[] | null): ReadonlySet<string> {
  return new Set((entries ?? []).map((entry) => `${entry.server}/${entry.tool}`));
}

function AddServer({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const action = useAction();

  return (
    <form
      className="form-row"
      onSubmit={(event) => {
        event.preventDefault();
        action.run(async () => {
          // The API probes before accepting and refuses a server it could not
          // reach, so there is no "added but unreachable" case to render: a
          // failure arrives as a refusal, with the probe's reason in it.
          const added = await api.addServer(name.trim(), target.trim());
          setName("");
          setTarget("");
          onAdded();
          return `${detailOf(added, `${added.server.name} added`)}. Nothing it offers is enabled yet.`;
        });
      }}
    >
      <label className="field">
        <span>Name</span>
        <input value={name} spellCheck={false} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field field--wide">
        <span>Target</span>
        <input
          value={target}
          spellCheck={false}
          placeholder="https://…"
          onChange={(e) => setTarget(e.target.value)}
        />
      </label>
      <button
        type="submit"
        className="button"
        disabled={action.busy || name.trim() === "" || target.trim() === ""}
      >
        {action.busy ? "Probing…" : "Add server"}
      </button>
      <ActionResult action={action} />
    </form>
  );
}

function AllowlistTable({
  entries,
  busy,
  onRemove,
}: {
  entries: AllowlistEntry[];
  busy: boolean;
  onRemove: (entry: AllowlistEntry) => void;
}) {
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Server</th>
          <th>Tool</th>
          <th>Effect</th>
          <th>Mutation</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr
            key={`${entry.server}/${entry.tool}`}
            className={toolMutates(entry) ? "row--mutating" : undefined}
          >
            <td>{entry.server}</td>
            <td>
              <code>{entry.tool}</code>
            </td>
            <td>
              <ToolEffectBadge tool={entry} />
            </td>
            <td>
              <YesNoBadge
                value={entry.mutation_enabled}
                yes="enabled"
                no="read-only"
                good={false}
              />
            </td>
            <td className="cell-actions">
              <ConfirmButton
                label="Revoke"
                confirmLabel="Revoke this tool"
                busy={busy}
                onConfirm={() => onRemove(entry)}
              />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function FederationScreen() {
  const servers = useResource(() => api.servers(), []);
  const allowlist = useResource(() => api.allowlist(), []);
  const action = useAction();
  const [pending, setPending] = useState<ToolChoice | null>(null);

  function refresh(): void {
    servers.reload();
    allowlist.reload();
  }

  function allow(choice: ToolChoice, confirmed: boolean): void {
    action.run(async () => {
      const result = await api.allow({
        server: choice.server,
        tool: choice.tool.name,
        read_only: !toolMutates(choice.tool),
        // Sent only when the operator typed the name. An enable that reaches
        // the API without it is refused there, which is what makes this a
        // gate rather than a decoration.
        ...(confirmed ? { confirm_tool_name: choice.tool.name } : {}),
      });
      setPending(null);
      refresh();
      const said = detailOf(result, `${choice.tool.name} is allowlisted`);
      // The warning is the API noticing that the effect an operator declared
      // and the one the server advertises disagree. It is the most important
      // sentence on this screen when it appears.
      const warning = result.warning ? ` ${result.warning}` : "";
      return confirmed
        ? `${said}. The escalation is recorded against your name.${warning}`
        : `${said}.${warning}`;
    });
  }

  function choose(choice: ToolChoice): void {
    action.clear();
    // Undeclared and undetermined both count as state-changing, so this is
    // the branch a server that declares nothing takes.
    if (toolMutates(choice.tool)) setPending(choice);
    else allow(choice, false);
  }

  return (
    <>
      <Panel
        title="Federated servers"
        description="Added here after a probe. An unreachable server offers the agent nothing."
        actions={
          <button type="button" className="button button--quiet" onClick={refresh}>
            Refresh
          </button>
        }
      >
        <AddServer onAdded={refresh} />
        <ActionResult action={action} />
        {pending !== null ? (
          <TypedConfirm
            toolName={pending.tool.name}
            serverName={pending.server}
            tool={pending.tool}
            busy={action.busy}
            onConfirm={() => allow(pending, true)}
            onCancel={() => setPending(null)}
          />
        ) : null}
        <Loaded resource={servers} empty="No federated servers. The agent reaches nothing outside itself.">
          {(list) => (
            <div className="cards">
              {list.map((server) => (
                <ServerCard
                  key={server.name}
                  server={server}
                  allowed={allowlistKeys(allowlist.data)}
                  busy={action.busy}
                  onAllow={choose}
                  onRemove={() =>
                    action.run(async () => {
                      const result = await api.removeServer(server.name);
                      refresh();
                      return detailOf(
                        result,
                        `${server.name} removed. Its tools are no longer offered.`,
                      );
                    })
                  }
                />
              ))}
            </div>
          )}
        </Loaded>
      </Panel>

      <Panel
        title="Allowlist"
        description="What the agent may actually call. Anything not listed here is not offered to it."
      >
        <Loaded resource={allowlist} empty="Nothing is allowlisted.">
          {(entries) => (
            <AllowlistTable
              entries={entries}
              busy={action.busy}
              onRemove={(entry) =>
                action.run(async () => {
                  const result = await api.disallow(entry.server, entry.tool);
                  refresh();
                  return detailOf(result, `${entry.tool} revoked.`);
                })
              }
            />
          )}
        </Loaded>
      </Panel>
    </>
  );
}
