/** What the last change said, success or refusal. */

import type { Action } from "../hooks/useAction";

export function ActionResult({ action }: { action: Action }) {
  if (action.error !== null) {
    return (
      <p className="problem" role="alert">
        {action.error}
      </p>
    );
  }
  if (action.note !== null) {
    return (
      <p className="note" role="status">
        {action.note}
      </p>
    );
  }
  return null;
}
