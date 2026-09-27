import { describe, expect, it } from "vitest";

import type {
  PersonQuestions,
  UsageGroup,
  UsageRow,
  UsageSummary,
  UsageWindow,
} from "../domain/types";
import { ApiError } from "../services/http";
import { UNAVAILABLE, UsageVM } from "./usage.svelte";

const NOW = Date.parse("2026-09-26T12:00:00Z");

function row(key: string, name: string | null = null): UsageRow {
  return {
    key,
    name,
    questions: 2,
    input_tokens: 100,
    output_tokens: 10,
    cost: 0.004,
    tool_calls: 1,
    tools: ["web_search"],
    voice_seconds: null,
  };
}

function summary(window: UsageWindow, group: UsageGroup, retention: number | null = 90): UsageSummary {
  return {
    ...window,
    group,
    label: "traced question runs",
    retention_days: retention,
    rows: [row(group === "person" ? "1001" : `${group}-a`, group === "person" ? "Ana" : null)],
    totals: { questions: 2, input_tokens: 100, output_tokens: 10, cost: 0.004, tool_calls: 1, voice_seconds: 0 },
  };
}

function page(page: number, total: number): PersonQuestions {
  return {
    from: "2026-08-28",
    to: "2026-09-26",
    person: { platform_user_id: "1001", person_id: 7, name: "Ana" },
    page,
    total_pages: total,
    hidden_before_notice: 3,
    questions: [
      {
        timestamp: "2026-09-20T10:00:00+00:00",
        feature: "corpus.fixed",
        tools: [],
        question: `question on page ${page}`,
        input_tokens: 10,
        output_tokens: 2,
        cost: 0.001,
      },
    ],
  };
}

function fakeApi(options: { down?: boolean; retention?: number | null } = {}) {
  const summaries: string[] = [];
  const questions: string[] = [];
  return {
    summaries,
    questions,
    usageSummary: async (window: UsageWindow, group: UsageGroup) => {
      summaries.push(`${window.from}..${window.to} ${group}`);
      if (options.down) throw new ApiError(503, "usage unavailable");
      return summary(window, group, options.retention === undefined ? 90 : options.retention);
    },
    personQuestions: async (id: string, window: UsageWindow, n: number) => {
      questions.push(`${id} ${window.from}..${window.to} page ${n}`);
      if (options.down) throw new ApiError(503, "usage unavailable");
      return page(n, 2);
    },
  };
}

describe("UsageVM", () => {
  it("reads every grouping for the last 30 days, and the retention the API states", async () => {
    const api = fakeApi();
    const vm = new UsageVM(api, () => false, () => NOW);
    await vm.load();
    expect(api.summaries).toEqual([
      "2026-08-28..2026-09-26 person",
      "2026-08-28..2026-09-26 feature",
      "2026-08-28..2026-09-26 model",
      "2026-08-28..2026-09-26 tool",
    ]);
    expect(vm.report.data?.person.rows[0]?.name).toBe("Ana");
    expect(vm.retentionDays).toBe(90);
  });

  it("states no retention period the API could not read", async () => {
    const vm = new UsageVM(fakeApi({ retention: null }), () => false, () => NOW);
    await vm.load();
    expect(vm.retentionDays).toBeNull();
  });

  it("says usage is unavailable rather than showing anything when the trace store is down", async () => {
    const vm = new UsageVM(fakeApi({ down: true }), () => false, () => NOW);
    await vm.load();
    expect(vm.report.error).toBe(UNAVAILABLE);
    expect(vm.report.data).toBeNull();
  });

  it("will not ask for a window the API would refuse", async () => {
    const api = fakeApi();
    const vm = new UsageVM(api, () => false, () => NOW);
    await vm.load();
    vm.from = "2026-01-01";
    expect(vm.problem).toBe("The window is at most 90 days.");
    expect(vm.canApply).toBe(false);
    await vm.apply();
    expect(api.summaries).toHaveLength(4);
  });

  it("reads a preset window at once", async () => {
    const api = fakeApi();
    const vm = new UsageVM(api, () => false, () => NOW);
    await vm.load();
    await vm.preset(7);
    expect(vm.shown).toEqual({ from: "2026-09-20", to: "2026-09-26" });
    expect(api.summaries.at(-1)).toBe("2026-09-20..2026-09-26 tool");
  });

  it("never asks for question text on behalf of somebody who may not read it", async () => {
    const api = fakeApi();
    const vm = new UsageVM(api, () => false, () => NOW);
    await vm.load();
    await vm.open(row("1001", "Ana"));
    expect(vm.questions).toBeNull();
    expect(api.questions).toEqual([]);
  });

  it("pages through a person's questions for the window the tables show", async () => {
    const api = fakeApi();
    const vm = new UsageVM(api, () => true, () => NOW);
    await vm.load();
    await vm.open(row("1001", "Ana"));
    const questions = vm.questions;
    expect(questions?.label).toBe("Ana");
    expect(questions?.questions.data?.hidden_before_notice).toBe(3);
    expect(questions?.canPrevious).toBe(false);
    await questions?.next();
    expect(questions?.questions.data?.questions[0]?.question).toBe("question on page 2");
    expect(questions?.canNext).toBe(false);
    await questions?.next();
    expect(api.questions).toEqual([
      "1001 2026-08-28..2026-09-26 page 1",
      "1001 2026-08-28..2026-09-26 page 2",
    ]);
  });

  it("drops question text when the panel closes or the window changes", async () => {
    const vm = new UsageVM(fakeApi(), () => true, () => NOW);
    await vm.load();
    await vm.open(row("1001"));
    expect(vm.questions?.label).toBe("1001");
    vm.close();
    expect(vm.questions).toBeNull();
    await vm.open(row("1001"));
    await vm.preset(7);
    expect(vm.questions).toBeNull();
  });

  it("says the questions are unavailable when the trace store is down", async () => {
    const vm = new UsageVM(fakeApi({ down: true }), () => true, () => NOW);
    await vm.open(row("1001"));
    expect(vm.questions?.questions.error).toBe(UNAVAILABLE);
  });
});
