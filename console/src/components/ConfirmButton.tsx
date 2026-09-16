/**
 * A destructive action that takes two clicks, in place.
 *
 * Not `window.confirm`: a native dialog is dismissed by muscle memory and
 * says nothing about what is being removed. This changes the button's own
 * label to name the consequence, which puts the question where the pointer
 * already is. Enabling a state-changing tool does not use this -- that needs
 * the typed gate, and a button you can click twice is not one.
 */

import { useState } from "react";

export function ConfirmButton({
  label,
  confirmLabel,
  busy,
  onConfirm,
}: {
  label: string;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);
  if (!armed) {
    return (
      <button
        type="button"
        className="button button--quiet"
        disabled={busy === true}
        onClick={() => setArmed(true)}
      >
        {label}
      </button>
    );
  }
  return (
    <span className="row row--tight">
      <button
        type="button"
        className="button button--danger"
        disabled={busy === true}
        onClick={() => {
          setArmed(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </button>
      <button type="button" className="button button--quiet" onClick={() => setArmed(false)}>
        Keep
      </button>
    </span>
  );
}
