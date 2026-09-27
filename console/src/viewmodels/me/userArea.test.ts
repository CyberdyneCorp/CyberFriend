import { describe, expect, it } from "vitest";

import { CONFIRM_WORD, isUserArea, retentionLines, type Erased, type MePrivacy, type MeSession } from "../../domain/me";
import type { UserApi } from "../../services/userApi";
import { MyFeatureRequestsVM } from "./myFeatureRequests.svelte";
import { UserAreaVM } from "./userArea.svelte";

const PRIVACY: MePrivacy = {
  known: true,
  archiving: true,
  facts: [{ kind: "email", value: "leo@example.com" }],
  memory: {},
  recent_questions: [],
  tasks: [],
  alerts: [],
  voice_seconds_this_month: 0,
  suggestions: [],
  tokens: [],
  traces: 0,
  retention: {
    tracing: true,
    trace_retention_days: 90,
    memory_retention_days: 30,
    backup_retention_days: null,
  },
  identity_account: "Your CyberdyneAuth account is not deleted.",
};

const ERASED: Erased = { mode: "erase", counts: { facts: 1 }, identity_account: "not deleted" };

interface Calls {
  erase: [string, string][];
  left: string[];
  suggested: string[];
}

function fakeApi(me: MeSession | null, calls: Calls, refuse?: string): UserApi {
  return {
    session: async () => me,
    privacy: async () => PRIVACY,
    suggestions: async () => [],
    suggest: async (text: string) => {
      if (refuse !== undefined) throw new Error(refuse);
      calls.suggested.push(text);
      return { outcome: "recorded", id: 1, message: "Thanks, your suggestion was recorded." };
    },
    erase: async (mode, confirm) => {
      calls.erase.push([mode, confirm]);
      return ERASED;
    },
    logout: async () => ({ signed_out: true }),
    loginUrl: () => "/auth/user/login",
    freshUrl: () => "/auth/user/fresh",
    leave: (url: string) => {
      calls.left.push(url);
    },
  };
}

function calls(): Calls {
  return { erase: [], left: [], suggested: [] };
}

describe("the user area", () => {
  it("is the #/me address and nothing else", () => {
    expect(isUserArea("/me")).toBe(true);
    expect(isUserArea("/me/privacy")).toBe(true);
    expect(isUserArea("/status")).toBe(false);
    expect(isUserArea("/meta")).toBe(false);
  });

  it("knows who is signed in and whether a profile is linked", async () => {
    const area = new UserAreaVM(fakeApi({ email: "leo@example.com", linked: false, fresh: false }, calls()));
    expect(area.session.loading).toBe(true);
    await area.session.start();
    expect(area.session.signedIn).toBe(true);
    expect(area.session.linked).toBe(false);
  });

  it("offers sign-in without a session", async () => {
    const area = new UserAreaVM(fakeApi(null, calls()));
    await area.session.start();
    expect(area.session.signedIn).toBe(false);
    expect(area.session.loginUrl).toBe("/auth/user/login");
  });
});

describe("the privacy dashboard", () => {
  it("states that no backups are kept when none are", () => {
    expect(retentionLines(PRIVACY.retention)).toContain("No database backups are kept.");
    expect(
      retentionLines({ ...PRIVACY.retention, backup_retention_days: 7 }).some((line) => line.includes("7 days")),
    ).toBe(true);
  });

  it("asks for a fresh sign-in before deleting, and sends nothing until then", async () => {
    const seen = calls();
    const area = new UserAreaVM(fakeApi({ email: null, linked: true, fresh: false }, seen));
    await area.session.start();
    const privacy = area.privacy();
    privacy.typed = CONFIRM_WORD;

    expect(privacy.needsFreshSignIn).toBe(true);
    expect(privacy.canErase()).toBe(false);
    await privacy.erase();
    expect(seen.erase).toEqual([]);

    privacy.signInAgain();
    expect(seen.left).toEqual(["/auth/user/fresh"]);
  });

  it("deletes with the chosen mode and the typed word, then ends the session", async () => {
    const seen = calls();
    const area = new UserAreaVM(fakeApi({ email: null, linked: true, fresh: true }, seen));
    await area.session.start();
    const privacy = area.privacy();
    privacy.mode = "erase_and_opt_out";

    privacy.typed = "delete";
    expect(privacy.canErase()).toBe(false);
    privacy.typed = ` ${CONFIRM_WORD} `;
    await privacy.erase();

    expect(seen.erase).toEqual([["erase_and_opt_out", CONFIRM_WORD]]);
    expect(area.erased).toEqual(ERASED);
    expect(area.session.signedIn).toBe(false);
  });
});

describe("my suggestions", () => {
  it("sends the draft and shows the server's answer", async () => {
    const seen = calls();
    const vm = new MyFeatureRequestsVM(fakeApi(null, seen));
    vm.draft = "a dark mode";
    await vm.submit();
    expect(seen.suggested).toEqual(["a dark mode"]);
    expect(vm.submitting.note).toBe("Thanks, your suggestion was recorded.");
    expect(vm.draft).toBe("");
  });

  it("shows a refusal with its reason and keeps the draft", async () => {
    const vm = new MyFeatureRequestsVM(fakeApi(null, calls(), "Suggestions can't include a phone number."));
    vm.draft = "call me on +55 11 98765 4321";
    await vm.submit();
    expect(vm.submitting.error).toBe("Suggestions can't include a phone number.");
    expect(vm.draft).toBe("call me on +55 11 98765 4321");
  });

  it("does not send an empty or over-long suggestion", () => {
    const vm = new MyFeatureRequestsVM(fakeApi(null, calls()));
    vm.draft = "   ";
    expect(vm.canSubmit).toBe(false);
    vm.draft = "x".repeat(1001);
    expect(vm.canSubmit).toBe(false);
  });
});
