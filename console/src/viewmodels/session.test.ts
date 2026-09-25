import { describe, expect, it } from "vitest";

import type { Principal } from "../domain/types";
import type { SessionPort } from "../services/session";
import type { SessionApi } from "../services/sessionApi";
import { SessionVM, SIGN_OUT_INCOMPLETE } from "./session.svelte";

const ADMIN: Principal = { subject: "ana-sub", display: "ana@example.com", roles: ["admin", "operator"], via: "oidc" };
const OPERATOR: Principal = { subject: "bo-sub", display: "bo@example.com", roles: ["operator"], via: "oidc" };
const TOKEN: Principal = { subject: "sam", display: "sam", roles: ["admin", "operator"], via: "token" };

type FakeSession = SessionPort & { held: () => string | null };

function fakeSession(): FakeSession {
  let principal: Principal | null = null;
  let token: string | null = null;
  const listeners = new Set<() => void>();
  const announce = () => listeners.forEach((listener) => listener());
  return {
    held: () => token,
    principal: () => principal,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    signIn: (who, value = null) => {
      principal = who;
      token = value;
      announce();
    },
    signOut: () => {
      principal = null;
      token = null;
      announce();
    },
  };
}

function fakeApi(overrides: Partial<SessionApi> = {}): SessionApi & { left: string[] } {
  const left: string[] = [];
  return {
    left,
    me: async () => null,
    config: async () => ({ sign_in: true }),
    loginUrl: () => "/auth/login",
    logout: async () => ({ end_session_url: "https://auth.example/end?id_token_hint=x" }),
    verifyToken: async () => TOKEN,
    leave: (url) => {
      left.push(url);
    },
    ...overrides,
  };
}

describe("SessionVM on load", () => {
  it("offers CyberdyneAuth first and keeps the token form behind a link", async () => {
    const vm = new SessionVM(fakeSession(), fakeApi());
    expect(vm.loading).toBe(true);
    await vm.start();
    expect(vm.loading).toBe(false);
    expect(vm.signInOffered).toBe(true);
    expect(vm.loginUrl).toBe("/auth/login");
    expect(vm.showTokenForm).toBe(false);
    vm.useToken();
    expect(vm.showTokenForm).toBe(true);
  });

  it("shows the token form alone when sign-in is not offered", async () => {
    const vm = new SessionVM(fakeSession(), fakeApi({ config: async () => ({ sign_in: false }) }));
    await vm.start();
    expect(vm.showTokenForm).toBe(true);
  });

  it("picks up a session the cookie already names", async () => {
    const session = fakeSession();
    const vm = new SessionVM(session, fakeApi({ me: async () => OPERATOR }));
    await vm.start();
    expect(vm.signedIn).toBe(true);
    expect(vm.role).toBe("operator");
    // Never a token for a cookie session.
    expect(session.held()).toBeNull();
  });

  it("says why when the API refuses the session outright", async () => {
    const vm = new SessionVM(
      fakeSession(),
      fakeApi({
        me: async () => {
          throw new Error("no console access");
        },
      }),
    );
    await vm.start();
    expect(vm.signedIn).toBe(false);
    expect(vm.error).toBe("no console access");
  });
});

describe("SessionVM roles", () => {
  it("lets an admin change things and an operator only read", () => {
    const session = fakeSession();
    const vm = new SessionVM(session, fakeApi());
    session.signIn(ADMIN);
    expect(vm.role).toBe("admin");
    expect(vm.canChange).toBe(true);
    session.signIn(OPERATOR);
    expect(vm.role).toBe("operator");
    expect(vm.canChange).toBe(false);
  });

  it("gives nothing to a principal with no console role", () => {
    const session = fakeSession();
    const vm = new SessionVM(session, fakeApi());
    session.signIn({ ...OPERATOR, roles: [] });
    expect(vm.role).toBeNull();
    expect(vm.canChange).toBe(false);
  });
});

describe("SessionVM break-glass token", () => {
  it("stays on the form with the refusal when the token is refused", async () => {
    const session = fakeSession();
    const vm = new SessionVM(
      session,
      fakeApi({
        verifyToken: async () => {
          throw new Error("That credential was refused.");
        },
      }),
    );
    vm.candidate = "cfa_wrong";
    await vm.submit();
    expect(vm.signedIn).toBe(false);
    expect(vm.error).toBe("That credential was refused.");
    // Never held, not even for the length of the check.
    expect(session.held()).toBeNull();
  });

  it("holds the token only after it was accepted, with the role the API gave it", async () => {
    const session = fakeSession();
    const checked: (string | null)[] = [];
    const vm = new SessionVM(
      session,
      fakeApi({
        verifyToken: async () => {
          checked.push(session.held());
          return { ...TOKEN, roles: ["operator"] };
        },
      }),
    );
    vm.candidate = "cfa_good";
    await vm.submit();
    expect(checked).toEqual([null]);
    expect(vm.signedIn).toBe(true);
    expect(vm.viaToken).toBe(true);
    // A token is operator once CyberdyneAuth is configured; the console follows the API.
    expect(vm.role).toBe("operator");
    // The form forgets what was typed.
    expect(vm.candidate).toBe("");
  });

  it("will not submit an empty token", async () => {
    let calls = 0;
    const vm = new SessionVM(
      fakeSession(),
      fakeApi({
        verifyToken: async () => {
          calls += 1;
          return TOKEN;
        },
      }),
    );
    vm.candidate = "   ";
    expect(vm.canSubmit).toBe(false);
    await vm.submit();
    expect(calls).toBe(0);
  });

  it("follows a sign-out it did not start, such as a 401", async () => {
    const session = fakeSession();
    const vm = new SessionVM(session, fakeApi());
    vm.candidate = "cfa_good";
    await vm.submit();
    session.signOut();
    expect(vm.signedIn).toBe(false);
    expect(vm.role).toBeNull();
    vm.dispose();
  });

  it("checks a token once, however often the form is submitted meanwhile", async () => {
    let calls = 0;
    let accept!: () => void;
    const vm = new SessionVM(
      fakeSession(),
      fakeApi({
        verifyToken: () => {
          calls += 1;
          return new Promise<Principal>((resolve) => (accept = () => resolve(TOKEN)));
        },
      }),
    );
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

  it("checks and holds the token without the whitespace it was pasted with", async () => {
    const session = fakeSession();
    const checked: string[] = [];
    const vm = new SessionVM(
      session,
      fakeApi({
        verifyToken: async (token) => {
          checked.push(token);
          return TOKEN;
        },
      }),
    );
    vm.candidate = "  cfa_good \n";
    await vm.submit();
    expect(checked).toEqual(["cfa_good"]);
    expect(session.held()).toBe("cfa_good");
  });

  it("keeps a token typed while the cookie check was still in flight", async () => {
    const session = fakeSession();
    let answer!: (who: Principal | null) => void;
    const vm = new SessionVM(
      session,
      fakeApi({ me: () => new Promise((resolve) => (answer = resolve)) }),
    );
    const loading = vm.start();
    vm.candidate = "cfa_good";
    await vm.submit();
    answer(OPERATOR);
    await loading;
    expect(vm.principal).toEqual(TOKEN);
  });
});

describe("SessionVM sign-out", () => {
  it("forgets a token without calling the server", async () => {
    let logouts = 0;
    const session = fakeSession();
    const api = fakeApi({
      logout: async () => {
        logouts += 1;
        return { end_session_url: null };
      },
    });
    const vm = new SessionVM(session, api);
    session.signIn(TOKEN, "cfa_x");
    await vm.signOut();
    expect(vm.signedIn).toBe(false);
    expect(logouts).toBe(0);
    expect(api.left).toEqual([]);
  });

  it("revokes a CyberdyneAuth session, then leaves for the provider's sign-out", async () => {
    const session = fakeSession();
    const order: string[] = [];
    const api = fakeApi({
      logout: async () => {
        order.push(`logout while ${session.principal() === null ? "signed out" : "signed in"}`);
        return { end_session_url: "https://auth.example/end?id_token_hint=x" };
      },
      leave: (url) => {
        order.push(`leave for ${url} while ${session.principal() === null ? "signed out" : "signed in"}`);
      },
    });
    const vm = new SessionVM(session, api);
    session.signIn(ADMIN);
    await vm.signOut();
    expect(order).toEqual([
      "logout while signed in",
      "leave for https://auth.example/end?id_token_hint=x while signed out",
    ]);
  });

  it("stays in the console when there is no provider sign-out to go to", async () => {
    const session = fakeSession();
    const api = fakeApi({ logout: async () => ({ end_session_url: null }) });
    const vm = new SessionVM(session, api);
    session.signIn(ADMIN);
    await vm.signOut();
    expect(vm.signedIn).toBe(false);
    expect(api.left).toEqual([]);
  });

  it("says so when the server did not hear the sign-out", async () => {
    const session = fakeSession();
    const api = fakeApi({
      logout: async () => {
        throw new Error("network down");
      },
    });
    const vm = new SessionVM(session, api);
    session.signIn(ADMIN);
    await vm.signOut();
    expect(vm.signedIn).toBe(false);
    expect(vm.error).toBe(SIGN_OUT_INCOMPLETE);
    expect(api.left).toEqual([]);
  });
});
