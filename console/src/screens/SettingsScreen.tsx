/** Every setting the API exposes, each showing where its value came from. */

import { api } from "../api/client";
import { Loaded, Panel } from "../components/DataState";
import { SettingsTable } from "../components/SettingsTable";
import { useResource } from "../hooks/useResource";

export function SettingsScreen() {
  const settings = useResource(() => api.settings(), []);
  return (
    <Panel
      title="Settings"
      description="Database beats environment beats default. The source column says which one is in force."
      actions={
        <button type="button" className="button button--quiet" onClick={settings.reload}>
          Refresh
        </button>
      }
    >
      <p className="muted">
        Credentials are not here and cannot be set here. The platform token, the
        model key and the database URL are read from the environment; the API
        refuses to store them and this console never displays them, masked or
        otherwise.
      </p>
      <Loaded resource={settings} empty="The API reports no settings.">
        {(rows) => <SettingsTable settings={rows} onSaved={settings.reload} />}
      </Loaded>
    </Panel>
  );
}
