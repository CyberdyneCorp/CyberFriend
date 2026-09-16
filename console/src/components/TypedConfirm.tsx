/**
 * The gate in front of enabling a tool that changes state.
 *
 * It is a typed confirmation and not a dialog, because a dialog is a thing
 * people click through. The operator has to read the name of the specific
 * tool they are granting and type it back; nothing else enables the button,
 * and there is no "enable anyway". The API refuses a mismatched
 * `confirm_tool_name` too -- this is the first of two gates saying the same
 * thing, not the only one.
 *
 * The effect badge is repeated inside the gate deliberately. The operator
 * chose this row on a list; the confirmation is the moment to say again, in
 * the same visual language, what kind of tool it is.
 */

import { useState } from "react";

import type { FederatedTool } from "../api/types";
import { confirmationMatches } from "../domain/effect";
import { ToolEffectBadge } from "./Badges";

export function TypedConfirm({
  toolName,
  serverName,
  tool,
  busy,
  onConfirm,
  onCancel,
}: {
  toolName: string;
  serverName: string;
  tool: Pick<FederatedTool, "effect" | "mutates">;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [typed, setTyped] = useState("");
  const matches = confirmationMatches(typed, toolName);

  return (
    <div className="confirm" role="group" aria-label={`Enable ${toolName}`}>
      <p>
        <strong>{toolName}</strong> on <strong>{serverName}</strong>{" "}
        <ToolEffectBadge tool={tool} />
      </p>
      <p className="muted">
        Enabling this lets the agent change something outside CyberFriend while
        answering a question. Type the tool's name to enable it. This is
        recorded as an escalation against your name.
      </p>
      <label className="field">
        <span>Type <code>{toolName}</code></span>
        <input
          autoFocus
          value={typed}
          spellCheck={false}
          autoComplete="off"
          onChange={(event) => setTyped(event.target.value)}
          aria-invalid={typed !== "" && !matches}
        />
      </label>
      <div className="row">
        <button
          type="button"
          className="button button--danger"
          disabled={!matches || busy}
          onClick={onConfirm}
        >
          {busy ? "Enabling…" : "Enable this tool"}
        </button>
        <button type="button" className="button button--quiet" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
