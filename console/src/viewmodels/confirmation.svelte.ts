/**
 * A removal that takes two clicks, in place.
 *
 * Not `window.confirm`: a native dialog is dismissed by muscle memory and says
 * nothing about what is being removed. The first click arms the control, which
 * then names the consequence where the pointer already is; only the second
 * sends anything. Enabling a state-changing tool does not use this -- that
 * needs the typed gate (`TypedConfirmVM`), and a button you can click twice is
 * not one.
 */

export class Confirmation {
  armed = $state(false);

  arm = (): void => {
    this.armed = true;
  };

  disarm = (): void => {
    this.armed = false;
  };

  /** The second click: disarm, then do it. Does nothing unless armed. */
  confirm = (work: () => void): void => {
    if (!this.armed) return;
    this.armed = false;
    work();
  };
}
