/**
 * Loading, refused and empty, told apart.
 *
 * "No federated servers" and "we could not ask" render as different things
 * here on purpose: on a screen whose job is to show what the agent may reach,
 * a failed request that looked like an empty list would be read as a system
 * that reaches nothing.
 */

import type { ReactNode } from "react";

import type { Resource } from "../hooks/useResource";

export function Problem({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="problem" role="alert">
      <span>{message}</span>
      {onRetry ? (
        <button type="button" className="button button--quiet" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function Loaded<T>({
  resource,
  empty,
  children,
}: {
  resource: Resource<T>;
  empty?: string;
  children: (data: T) => ReactNode;
}) {
  if (resource.error !== null) {
    return <Problem message={resource.error} onRetry={resource.reload} />;
  }
  if (resource.data === null) {
    return <p className="muted">{resource.loading ? "Loading…" : "Nothing loaded."}</p>;
  }
  if (Array.isArray(resource.data) && resource.data.length === 0) {
    return <p className="muted">{empty ?? "Nothing here."}</p>;
  }
  return <>{children(resource.data)}</>;
}

export function Panel({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      <header className="panel__head">
        <div>
          <h2>{title}</h2>
          {description ? <p className="muted">{description}</p> : null}
        </div>
        {actions ? <div className="panel__actions">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}
