/**
 * Every setting with its provenance, and an edit box where one is allowed.
 *
 * The source badge is not decoration. A setting saved here but overridden by
 * an environment variable looks exactly like one that did not save, and the
 * operator's next move -- edit it again, or go and change the deployment --
 * differs completely between the two. So the badge is on every row, and a row
 * whose value comes from the environment says what editing it will and will
 * not do.
 */

import { useState } from "react";

import { api } from "../api/client";
import type { Setting } from "../api/types";
import { SourceBadge } from "./Badges";
import { formatTime, formatValue } from "./format";
import { settingText, sourceOf } from "../domain/settings";
import { useAction } from "../hooks/useAction";
import { ActionResult } from "./ActionResult";

function Row({ setting, onSaved }: { setting: Setting; onSaved: () => void }) {
  const [typed, setTyped] = useState<string | null>(null);
  const action = useAction();
  const editing = typed !== null;

  function save(value: string): void {
    action.run(async () => {
      const saved = await api.putSetting(setting.key, value);
      setTyped(null);
      onSaved();
      // The provenance that comes back is the one that matters: an edit that
      // stored fine and is still overridden by the environment reports itself
      // as `environment`, and saying "Saved." there would be a lie the
      // operator only discovers when the behaviour does not change.
      return sourceOf(saved.source) === "environment"
        ? "Stored, but an environment variable still overrides it. It will not take effect until that variable is removed from the deployment."
        : "Saved.";
    });
  }

  return (
    <tr>
      <td>
        <code>{setting.key}</code>
        {setting.summary ? <p className="muted">{setting.summary}</p> : null}
      </td>
      <td>
        {editing ? (
          <input
            autoFocus
            className="cell-input"
            value={typed}
            spellCheck={false}
            onChange={(event) => setTyped(event.target.value)}
          />
        ) : (
          <span className="value">{formatValue(setting.value)}</span>
        )}
        <ActionResult action={action} />
      </td>
      <td>
        <SourceBadge source={setting.source} />
        {setting.updated_by ? (
          <p className="muted">
            {setting.updated_by} · {formatTime(setting.updated_at)}
          </p>
        ) : null}
      </td>
      <td className="cell-actions">
        {!setting.editable ? (
          <span className="muted">
            {setting.managed_by ? `managed at ${setting.managed_by}` : "not editable here"}
          </span>
        ) : editing ? (
          <span className="row row--tight">
            <button
              type="button"
              className="button"
              disabled={action.busy}
              onClick={() => save(typed)}
            >
              Save
            </button>
            <button
              type="button"
              className="button button--quiet"
              onClick={() => setTyped(null)}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="button button--quiet"
            onClick={() => setTyped(settingText(setting))}
          >
            Edit
          </button>
        )}
      </td>
    </tr>
  );
}

export function SettingsTable({
  settings,
  onSaved,
}: {
  settings: Setting[];
  onSaved: () => void;
}) {
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Setting</th>
          <th>Value</th>
          <th>Source</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {settings.map((setting) => (
          <Row key={setting.key} setting={setting} onSaved={onSaved} />
        ))}
      </tbody>
    </table>
  );
}
