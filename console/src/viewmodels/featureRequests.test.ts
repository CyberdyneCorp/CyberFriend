import { describe, expect, it } from "vitest";

import type { FeatureRequest, FeatureRequestPatch, FeatureRequestStatus } from "../domain/types";
import { FeatureRequestsVM, NOTHING_CHANGED } from "./featureRequests.svelte";

const ROW: FeatureRequest = {
  id: 12,
  text: "dark mode",
  language: "en",
  status: "new",
  admin_note: null,
  duplicate_of: null,
  source_kind: "command",
  person: "Sam",
  same_text_elsewhere: 0,
  created_at: "2026-09-15T10:00:00+00:00",
  updated_at: "2026-09-15T10:00:00+00:00",
  updated_by: null,
};

function fakeApi(total = 1) {
  const asked: [FeatureRequestStatus | null, number][] = [];
  const patched: [number, FeatureRequestPatch][] = [];
  const api = {
    featureRequests: async (status: FeatureRequestStatus | null, page: number) => {
      asked.push([status, page]);
      return { items: [ROW], total, page, page_size: 50 };
    },
    triageFeatureRequest: async (id: number, patch: FeatureRequestPatch) => {
      patched.push([id, patch]);
      return { changed: `feature_request.${id}`, request: { ...ROW, ...patch } as FeatureRequest };
    },
  };
  return { api, asked, patched };
}

describe("FeatureRequestsVM", () => {
  it("asks for one status from the first page", async () => {
    const { api, asked } = fakeApi();
    const vm = new FeatureRequestsVM(api);
    await vm.load();
    vm.show("planned");
    expect(asked).toEqual([
      [null, 1],
      ["planned", 1],
    ]);
  });

  it("turns pages within the total", async () => {
    const { api, asked } = fakeApi(120);
    const vm = new FeatureRequestsVM(api);
    await vm.load();
    expect(vm.pages).toBe(3);
    vm.turn(-1);
    vm.turn(1);
    expect(asked.map(([, page]) => page)).toEqual([1, 2]);
  });

  it("sends only the fields the admin changed", async () => {
    const { api, patched } = fakeApi();
    const vm = new FeatureRequestsVM(api);
    vm.edit(ROW);
    if (vm.draft === null) throw new Error("no draft");
    vm.draft.status = "planned";
    await vm.save(ROW);
    expect(patched).toEqual([[12, { status: "planned" }]]);
    expect(vm.action.note).toBe("#12 saved.");
    expect(vm.draft).toBeNull();
  });

  it("sends nothing for an untouched draft", async () => {
    const { api, patched } = fakeApi();
    const vm = new FeatureRequestsVM(api);
    vm.edit(ROW);
    await vm.save(ROW);
    expect(patched).toEqual([]);
    expect(vm.action.note).toBe(NOTHING_CHANGED);
  });

  it("keeps the draft and says why when it cannot be sent", async () => {
    const { api, patched } = fakeApi();
    const vm = new FeatureRequestsVM(api);
    vm.edit(ROW);
    if (vm.draft === null) throw new Error("no draft");
    vm.draft.duplicateOf = "12";
    await vm.save(ROW);
    expect(patched).toEqual([]);
    expect(vm.action.error).toMatch(/itself/);
    expect(vm.draft).not.toBeNull();
  });

  it("keeps the typed draft when the server refuses the change", async () => {
    const { api } = fakeApi();
    const refusing = {
      ...api,
      triageFeatureRequest: async () => {
        throw new Error("duplicate_of names a suggestion that does not exist");
      },
    };
    const vm = new FeatureRequestsVM(refusing);
    vm.edit(ROW);
    if (vm.draft === null) throw new Error("no draft");
    vm.draft.status = "planned";
    vm.draft.note = "next quarter";
    await vm.save(ROW);
    expect(vm.action.error).toMatch(/does not exist/);
    expect(vm.draft).toMatchObject({ id: 12, status: "planned", note: "next quarter" });
  });
});
