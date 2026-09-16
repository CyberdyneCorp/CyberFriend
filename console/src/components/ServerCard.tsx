/**
 * One federated server: whether it answered, and what it offers.
 *
 * Reachability is shown per server and never inferred from an empty tool
 * list: a server that is down and a server that offers nothing are different
 * problems with different fixes, and the second is not fixed by waiting.
 *
 * A server's *claim* to be read-only is shown next to the effect the agent
 * will actually treat the tool as having, because the claim grants nothing by
 * itself -- the operator's allowlisting does.
 */

import type { FederatedTool, FederationServer } from "../api/types";
import { toolMutates } from "../domain/effect";
import { ToolEffectBadge, YesNoBadge } from "./Badges";
import { ConfirmButton } from "./ConfirmButton";

export interface ToolChoice {
  server: string;
  tool: FederatedTool;
}

function ToolRow({
  server,
  tool,
  allowlisted,
  busy,
  onAllow,
}: {
  server: string;
  tool: FederatedTool;
  allowlisted: boolean;
  busy: boolean;
  onAllow: (choice: ToolChoice) => void;
}) {
  return (
    <li className={toolMutates(tool) ? "tool tool--mutating" : "tool"}>
      <span className="tool__name">{tool.name}</span>
      <ToolEffectBadge tool={tool} />
      {tool.claims_read_only && toolMutates(tool) ? (
        <span className="muted">the server claims read-only</span>
      ) : null}
      {allowlisted ? (
        <span className="muted">
          {tool.mutation_enabled ? "allowed, mutation enabled" : "on the allowlist"}
        </span>
      ) : (
        <button
          type="button"
          className="button button--quiet"
          disabled={busy}
          onClick={() => onAllow({ server, tool })}
        >
          Allow
        </button>
      )}
    </li>
  );
}

export function ServerCard({
  server,
  allowed,
  busy,
  onAllow,
  onRemove,
}: {
  server: FederationServer;
  /** `server/tool` keys already on the allowlist, for an API that sent no flag. */
  allowed: ReadonlySet<string>;
  busy: boolean;
  onAllow: (choice: ToolChoice) => void;
  onRemove: () => void;
}) {
  return (
    <article className="card">
      <header className="card__head">
        <div>
          <h3>{server.name}</h3>
          <p className="muted">
            <code>{server.target}</code>
          </p>
        </div>
        <div className="row row--tight">
          <YesNoBadge value={server.reachable} yes="reachable" no="unreachable" />
          <ConfirmButton
            label="Remove"
            confirmLabel="Remove this server"
            busy={busy}
            onConfirm={onRemove}
          />
        </div>
      </header>
      {server.failure ? <p className="problem">{server.failure}</p> : null}
      {server.tools.length === 0 ? (
        <p className="muted">
          {server.reachable
            ? "Reachable, and offers no tools."
            : "No tools discovered; the last probe did not reach it."}
        </p>
      ) : (
        <ul className="tools">
          {server.tools.map((tool) => (
            <ToolRow
              key={tool.name}
              server={server.name}
              tool={tool}
              allowlisted={tool.allowlisted ?? allowed.has(`${server.name}/${tool.name}`)}
              busy={busy}
              onAllow={onAllow}
            />
          ))}
        </ul>
      )}
    </article>
  );
}
