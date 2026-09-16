/**
 * How long anything is kept, and who has asked to be left out of it.
 *
 * The two belong on one screen because they are the same question asked from
 * two directions: retention is what the archive forgets on a clock, an
 * opt-out is what it never records in the first place.
 */

import { useState } from "react";

import { api, detailOf } from "../api/client";
import { ActionResult } from "../components/ActionResult";
import { ConfirmButton } from "../components/ConfirmButton";
import { Loaded, Panel } from "../components/DataState";
import { SettingsTable } from "../components/SettingsTable";
import { formatTime } from "../components/format";
import { parsePerson } from "../domain/person";
import { isRetentionSetting } from "../domain/settings";
import { useAction } from "../hooks/useAction";
import { useResource } from "../hooks/useResource";

function RetentionSettings() {
  const settings = useResource(() => api.settings(), []);
  return (
    <Panel
      title="Retention"
      description="How long messages, windows, asks and documents are kept."
    >
      <Loaded resource={settings}>
        {(rows) => {
          const retention = rows.filter((setting) => isRetentionSetting(setting.key));
          if (retention.length === 0) {
            return (
              <p className="muted">
                The API exposes no retention setting, so retention is whatever
                the deployment configured. Everything said in an indexed channel
                is kept until then.
              </p>
            );
          }
          return <SettingsTable settings={retention} onSaved={settings.reload} />;
        }}
      </Loaded>
    </Panel>
  );
}

function AddOptOut({ onAdded }: { onAdded: () => void }) {
  const [platform, setPlatform] = useState("discord");
  const [userId, setUserId] = useState("");
  const action = useAction();

  return (
    <form
      className="form-row"
      onSubmit={(event) => {
        event.preventDefault();
        action.run(async () => {
          const result = await api.addOptOut(platform.trim(), userId.trim());
          setUserId("");
          onAdded();
          return detailOf(result, "Opted out. Nothing further from them is recorded.");
        });
      }}
    >
      <label className="field">
        <span>Platform</span>
        <input value={platform} onChange={(event) => setPlatform(event.target.value)} />
      </label>
      <label className="field">
        <span>Platform user id</span>
        <input
          value={userId}
          spellCheck={false}
          placeholder="e.g. 1234567890"
          onChange={(event) => setUserId(event.target.value)}
        />
      </label>
      <button type="submit" className="button" disabled={action.busy || userId.trim() === ""}>
        {action.busy ? "Saving…" : "Opt this person out"}
      </button>
      <ActionResult action={action} />
    </form>
  );
}

function OptOuts() {
  const optOuts = useResource(() => api.optOuts(), []);
  const action = useAction();

  return (
    <Panel
      title="Opt-outs"
      description="People whose messages are not recorded and not retrievable."
    >
      <AddOptOut onAdded={optOuts.reload} />
      <ActionResult action={action} />
      <Loaded resource={optOuts} empty="Nobody has opted out.">
        {(people) => (
          <table className="table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Since</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {people.map((entry) => {
                // `person` arrives as `platform:id`; the endpoint that
                // reverses an opt-out takes the two separately. A row this
                // cannot read gets a disabled button and a reason, never a
                // guessed request -- a guessed id opts the wrong person in.
                const person = parsePerson(entry.person);
                return (
                  <tr key={entry.person}>
                    <td>{entry.person}</td>
                    <td>{formatTime(entry.since)}</td>
                    <td className="cell-actions">
                      {person === null ? (
                        <span className="muted">this row cannot be read as a person</span>
                      ) : (
                        <ConfirmButton
                          label="Opt back in"
                          confirmLabel="Record them again"
                          busy={action.busy}
                          onConfirm={() =>
                            action.run(async () => {
                              const result = await api.removeOptOut(
                                person.platform,
                                person.id,
                              );
                              optOuts.reload();
                              return detailOf(
                                result,
                                `${entry.person} is recorded again from now on.`,
                              );
                            })
                          }
                        />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Loaded>
    </Panel>
  );
}

export function RetentionScreen() {
  return (
    <>
      <RetentionSettings />
      <OptOuts />
    </>
  );
}
