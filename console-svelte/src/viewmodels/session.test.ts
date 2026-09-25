import { describe, expect, it } from "vitest";

import type { SessionPort } from "../services/session";
import { SessionVM } from "./session.svelte";

function fakeSession(): SessionPort & { held: () => string | null } {
  let token: string | null = null;
  const listeners = new Set<() => void>();
  const announce = () => listeners.forEach((listener) => listener());
  return {
    held: () => token,
    isSignedIn: () => token !== null,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    signIn: (value) => {
      token = value;
      announce();
    },
    signOut: () => {
      token = null;
      announce();
    },
  };
}

describe("SessionVM", () => {
  it("stays on the form with the refusal when the credential is refused", async () => {
    const session = fakeSession();
    const vm = new SessionVM(session, async () => {
      throw new Error("That credential was refused.");
    });
    vm.candidate = "cfa_wrong";
    await vm.submit();
    expect(vm.signedIn).toBe(false);
    expect(vm.error).toBe("That credential was refused.");
    // Never held, not even for the length of the check.
    expect(session.held()).toBeNull();
  });

  it("holds the credential only after it was accepted", async () => {
    const session = fakeSession();
    const checked: (string | null)[] = [];
    const vm = new SessionVM(session, async () => {
      checked.push(session.held());
    });
    vm.candidate = "cfa_good";
    await vm.submit();
    expect(checked).toEqual([null]);
    expect(vm.signedIn).toBe(true);
    expect(vm.role).toBe("admin");
    // The form forgets what was typed.
    expect(vm.candidate).toBe("");
  });

  it("will not submit an empty credential", async () => {
    let calls = 0;
    const vm = new SessionVM(fakeSession(), async () => {
      calls += 1;
    });
    vm.candidate = "   ";
    expect(vm.canSubmit).toBe(false);
    await vm.submit();
    expect(calls).toBe(0);
  });

  it("follows a sign-out it did not start, such as a 401", async () => {
    const session = fakeSession();
    const vm = new SessionVM(session, async () => {});
    vm.candidate = "cfa_good";
    await vm.submit();
    session.signOut();
    expect(vm.signedIn).toBe(false);
    expect(vm.role).toBeNull();
    vm.dispose();
  });

  it("checks a credential once, however often the form is submitted meanwhile", async () => {
    let calls = 0;
    let accept!: () => void;
    const vm = new SessionVM(fakeSession(), () => {
      calls += 1;
      return new Promise<void>((resolve) => (accept = resolve));
    });
    vm.candidate = "cfa_good";
    const first = vm.submit();
    expect(vm.checking).toBe(true);
    expect(vm.canSubmit).toBe(false);
    await vm.submit();
    accept();
    await first;
    expect(calls).toBe(1);
    expect(vm.checking).toBe(false);
  });

  it("checks and holds the credential without the whitespace it was pasted with", async () => {
    const session = fakeSession();
    const checked: string[] = [];
    const vm = new SessionVM(session, async (token) => {
      checked.push(token);
    });
    vm.candidate = "  cfa_good \n";
    await vm.submit();
    expect(checked).toEqual(["cfa_good"]);
    expect(session.held()).toBe("cfa_good");
  });
});
