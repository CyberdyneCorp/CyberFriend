/**
 * MCP credentials: review them, and revoke one.
 *
 * There is no issue form, and its absence is the point. An MCP credential is
 * one person's read of the corpus, so minting one from this console would let
 * an operator holding only a `cfa_` token mint a `cfm_` one for any Discord
 * account and read what that account can see -- a route to message content
 * through the surface documented as having none. Issuing therefore stays in a
 * shell on the host, where it needs the agent's own environment:
 *
 *     python -m chatmemory.mcp.issue_token issue <discord_user_id> --label x
 *
 * Revoking stays here, because taking access away is what an operator needs at
 * 3am and it cannot widen anything.
 */

import { api } from "../api/client";
import { ActionResult } from "../components/ActionResult";
import { ConfirmButton } from "../components/ConfirmButton";
import { Loaded, Panel } from "../components/DataState";
import { formatTime } from "../components/format";
import { useAction } from "../hooks/useAction";
import { useResource } from "../hooks/useResource";

export function TokensScreen() {
  const tokens = useResource(() => api.tokens(), []);
  const action = useAction();

  return (
    <Panel
      title="Tokens"
      description="A token is one person's view of the corpus. It never widens what they may read."
      actions={
        <button type="button" className="button button--quiet" onClick={tokens.reload}>
          Refresh
        </button>
      }
    >
      <p className="muted">
        Credentials are issued from a shell on the host with{" "}
        <code>python -m chatmemory.mcp.issue_token</code>. This console can
        review and revoke them; it cannot mint one, because a new credential
        would read everything that Discord account can see.
      </p>
      <ActionResult action={action} />
      <Loaded resource={tokens} empty="No tokens have been issued.">
        {(rows) => (
          <table className="table">
            <thead>
              <tr>
                <th>Label</th>
                <th>Person</th>
                <th>Issued</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((token, index) => (
                <tr key={token.id ?? `${token.person}-${token.issued_at}-${index}`}>
                  <td>{token.label || <span className="muted">unlabelled</span>}</td>
                  <td>{token.person}</td>
                  <td>{formatTime(token.issued_at)}</td>
                  <td>
                    {token.revoked_at ? (
                      <span className="badge badge--warn">
                        revoked {formatTime(token.revoked_at)}
                      </span>
                    ) : (
                      <span className="badge badge--ok">live</span>
                    )}
                  </td>
                  <td className="cell-actions">
                    {token.revoked_at ? null : token.id ? (
                      <ConfirmButton
                        label="Revoke"
                        confirmLabel="Revoke this token"
                        busy={action.busy}
                        onConfirm={() =>
                          action.run(async () => {
                            await api.revokeToken(token.id as string);
                            tokens.reload();
                            return "Revoked. It stops working immediately.";
                          })
                        }
                      />
                    ) : (
                      <span className="muted">no id on this row</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Loaded>
    </Panel>
  );
}
