// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it } from "vitest";

import type { MeSession } from "../../domain/me";
import type { UserApi } from "../../services/userApi";
import { UserAreaVM } from "../../viewmodels/me/userArea.svelte";
import MeApp from "./MeApp.svelte";

function area(me: MeSession | null): UserAreaVM {
  const api = {
    session: async () => me,
    privacy: async () => {
      throw new Error("not linked");
    },
    suggestions: async () => [],
    unlink: async () => ({ unlinked: true }),
    loginUrl: () => "/auth/user/login",
    freshUrl: () => "/auth/user/fresh",
  } as unknown as UserApi;
  return new UserAreaVM(api);
}

afterEach(() => cleanup());

describe("the user area's page", () => {
  it("offers sign-in without a session", async () => {
    render(MeApp, { props: { area: area(null) } });
    const link = await screen.findByRole("link", { name: "Sign in with CyberdyneAuth" });
    expect(link.getAttribute("href")).toBe("/auth/user/login");
  });

  it("says how to link when the account has no profile", async () => {
    render(MeApp, { props: { area: area({ email: "x@example.com", linked: false, discord: null, fresh: false }) } });
    expect(await screen.findByText(/No CyberFriend profile is linked/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Privacy" })).toBeNull();
  });

  it("names the linked Discord account and unlinks on request", async () => {
    const me: MeSession = { email: "leo@example.com", linked: true, discord: "Ana (Discord user 8)", fresh: false };
    render(MeApp, { props: { area: area(me) } });
    expect(await screen.findByText("Ana (Discord user 8)")).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: "Unlink" }));

    expect(await screen.findByRole("heading", { name: "Unlinked" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Privacy" })).toBeNull();
  });
});
