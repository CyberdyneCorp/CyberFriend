/**
 * What is indexed, and whether the bot can actually read it.
 *
 * Those are two different facts and the screen keeps them apart. A channel in
 * scope that the bot cannot read produces no error anywhere -- it simply
 * indexes nothing -- and "why is this channel not searchable" is otherwise a
 * question with no answer on any screen.
 */

import { useState } from "react";

import { api, detailOf } from "../api/client";
import { ActionResult } from "../components/ActionResult";
import { YesNoBadge } from "../components/Badges";
import { ConfirmButton } from "../components/ConfirmButton";
import { Loaded, Panel } from "../components/DataState";
import { useAction } from "../hooks/useAction";
import { useResource } from "../hooks/useResource";

function AddChannel({ onAdded }: { onAdded: () => void }) {
  const [id, setId] = useState("");
  const action = useAction();
  return (
    <form
      className="form-row"
      onSubmit={(event) => {
        event.preventDefault();
        action.run(async () => {
          const added = await api.addChannel(id.trim());
          const channel = added.channel;
          setId("");
          onAdded();
          const said = detailOf(added, `${channel.name || channel.id} is now in scope`);
          // Added and unreadable is the case worth saying out loud: it is
          // accepted configuration that will index nothing, and it produces
          // no error anywhere else.
          return channel.readable_by_bot
            ? said
            : `${said} — but the bot cannot read it, so nothing will be indexed until it is given access.`;
        });
      }}
    >
      <label className="field">
        <span>Channel id</span>
        <input
          value={id}
          spellCheck={false}
          placeholder="e.g. 1234567890"
          onChange={(event) => setId(event.target.value)}
        />
      </label>
      <button type="submit" className="button" disabled={action.busy || id.trim() === ""}>
        {action.busy ? "Checking…" : "Index this channel"}
      </button>
      <ActionResult action={action} />
    </form>
  );
}

export function ChannelsScreen() {
  const channels = useResource(() => api.channels(), []);
  const action = useAction();

  return (
    <Panel
      title="Channels"
      description="Everything said in an indexed channel becomes searchable, subject to who may read it."
      actions={
        <button type="button" className="button button--quiet" onClick={channels.reload}>
          Refresh
        </button>
      }
    >
      <AddChannel onAdded={channels.reload} />
      <ActionResult action={action} />
      <Loaded resource={channels} empty="No channels are indexed.">
        {(rows) => (
          <table className="table">
            <thead>
              <tr>
                <th>Channel</th>
                <th>Id</th>
                <th>Indexed</th>
                <th>Bot access</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((channel) => (
                <tr key={channel.id}>
                  <td>{channel.name || <span className="muted">unnamed</span>}</td>
                  <td>
                    <code>{channel.id}</code>
                  </td>
                  <td>
                    <YesNoBadge value={channel.indexed} yes="indexed" no="not indexed" />
                  </td>
                  <td>
                    <YesNoBadge value={channel.readable_by_bot} yes="can read" no="cannot read" />
                    {channel.evidence ? <p className="muted">{channel.evidence}</p> : null}
                  </td>
                  <td className="cell-actions">
                    <ConfirmButton
                      label="Stop indexing"
                      confirmLabel="Stop indexing it"
                      busy={action.busy}
                      onConfirm={() =>
                        action.run(async () => {
                          const result = await api.removeChannel(channel.id);
                          channels.reload();
                          return detailOf(
                            result,
                            `${channel.name || channel.id} is out of scope.`,
                          );
                        })
                      }
                    />
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
