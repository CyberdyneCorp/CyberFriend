/**
 * Who changed what, when, and from what to what.
 *
 * Configuration here changes without a pull request, so this record is the
 * only remaining review -- which is why it carries the before and the after
 * rather than the fact of a change. Escalations are filterable rather than
 * findable by reading: "what got more permissive, and who did it" is the
 * question this screen exists for.
 */

import { useState } from "react";

import { api } from "../api/client";
import { KindBadge } from "../components/Badges";
import { Loaded, Panel } from "../components/DataState";
import { formatTime } from "../components/format";
import type { ChangeKind } from "../api/types";
import { useResource } from "../hooks/useResource";

const FILTERS: (ChangeKind | "all")[] = ["all", "escalation", "refused", "applied"];

export function AuditScreen() {
  const audit = useResource(() => api.audit(), []);
  const [filter, setFilter] = useState<ChangeKind | "all">("all");

  return (
    <Panel
      title="Audit"
      description="Append-only. Nothing in this console can edit or remove an entry."
      actions={
        <div className="row row--tight">
          {FILTERS.map((kind) => (
            <button
              key={kind}
              type="button"
              className={`button button--quiet${filter === kind ? " button--on" : ""}`}
              onClick={() => setFilter(kind)}
            >
              {kind}
            </button>
          ))}
        </div>
      }
    >
      <Loaded resource={audit} empty="Nothing has been changed yet.">
        {(entries) => {
          const shown = entries.filter((e) => filter === "all" || e.kind === filter);
          if (shown.length === 0) return <p className="muted">No {filter} changes.</p>;
          return (
            <table className="table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Operator</th>
                  <th>Setting</th>
                  <th>Before</th>
                  <th>After</th>
                  <th>Kind</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((entry, index) => (
                  <tr
                    key={`${entry.at}-${entry.setting}-${index}`}
                    className={entry.kind === "escalation" ? "row--mutating" : undefined}
                  >
                    <td>{formatTime(entry.at)}</td>
                    <td>{entry.operator}</td>
                    <td>
                      <code>{entry.setting}</code>
                      {entry.reason ? <p className="muted">{entry.reason}</p> : null}
                    </td>
                    <td className="cell-value">{entry.before ?? <span className="muted">—</span>}</td>
                    <td className="cell-value">{entry.after ?? <span className="muted">—</span>}</td>
                    <td>
                      <KindBadge kind={entry.kind} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          );
        }}
      </Loaded>
    </Panel>
  );
}
