/**
 * Health, ingestion progress, embedding backlog, and what changed recently.
 *
 * Rendered from whatever the status endpoint reports rather than from a fixed
 * list of field names. The contract fixes the categories, not the fields
 * inside them, and the failure of hard-coding guessed names is silent: a card
 * of dashes where the number an operator came here for should be.
 *
 * Counts and timings only. Nothing on this screen is corpus content, and
 * there is no endpoint here that could return any.
 */

import { api } from "../api/client";
import type { JsonValue } from "../api/types";
import { Loaded, Panel } from "../components/DataState";
import { KindBadge } from "../components/Badges";
import { formatRelative, formatValue, humanise, partition } from "../components/format";
import { useResource } from "../hooks/useResource";

function Metrics({ entries }: { entries: [string, JsonValue][] }) {
  if (entries.length === 0) return <p className="muted">Nothing reported.</p>;
  return (
    <dl className="metrics">
      {entries.map(([key, value]) => (
        <div key={key} className="metric">
          <dt>{humanise(key)}</dt>
          <dd>{formatValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Group({ name, value }: { name: string; value: JsonValue }) {
  const { scalars, groups } = partition(value);
  return (
    <Panel title={humanise(name)}>
      <Metrics entries={scalars} />
      {groups.map(([key, nested]) => (
        <div key={key} className="subgroup">
          <h3>{humanise(key)}</h3>
          <Metrics entries={partition(nested).scalars} />
        </div>
      ))}
    </Panel>
  );
}

function RecentChanges() {
  const audit = useResource(() => api.audit(), []);
  return (
    <Panel title="Recent changes" description="The last configuration changes, newest first.">
      <Loaded resource={audit} empty="No configuration has been changed yet.">
        {(entries) => (
          <ul className="changes">
            {entries.slice(0, 8).map((entry, index) => (
              <li key={`${entry.at}-${entry.setting}-${index}`}>
                <KindBadge kind={entry.kind} />
                <span className="changes__setting">{entry.setting}</span>
                <span className="muted">
                  {entry.operator} · {formatRelative(entry.at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Loaded>
    </Panel>
  );
}

export function StatusScreen() {
  const status = useResource(() => api.status(), []);
  return (
    <>
      <Loaded resource={status}>
        {(report) => {
          const { scalars, groups } = partition(report);
          return (
            <>
              {scalars.length > 0 ? (
                <Panel title="Health">
                  <Metrics entries={scalars} />
                </Panel>
              ) : null}
              {groups.map(([name, value]) => (
                <Group key={name} name={name} value={value} />
              ))}
            </>
          );
        }}
      </Loaded>
      <RecentChanges />
    </>
  );
}
