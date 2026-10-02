import { describe, expect, it } from "vitest";

import type { ExecutionDetail, NodeExecution } from "@/lib/types";

import { buildTimeline, edgeState, formatOffset, stateAt } from "./replay-model";

const T0 = Date.parse("2026-10-02T04:15:14.986Z");
const at = (ms: number) => new Date(T0 + ms).toISOString();

function row(node_key: string, start: number | null, end: number | null, status: NodeExecution["status"] = "success"): NodeExecution {
  return {
    id: node_key, node_id: null, node_key, position: 0, node_type: "text", node_label: node_key.toUpperCase(), status,
    input: null, output: null, error_message: status === "failed" ? "boom" : null,
    started_at: start === null ? null : at(start), finished_at: end === null ? null : at(end), duration_ms: null,
    queue: null, worker_hostname: null,
  } as NodeExecution;
}

// The Meeting Notes run: queued 129 ms, a 228 ms hand-off between the audio and llm workers.
const run = {
  started_at: at(0),
  finished_at: at(5314),
  node_executions: [
    row("notes", 1948, 3681),
    row("input", 129, 166),
    row("stt", 185, 1720),
    row("never", null, null, "skipped"),
  ],
} as unknown as ExecutionDetail;

describe("buildTimeline", () => {
  it("measures every step from the run's start and keeps the real gaps", () => {
    const timeline = buildTimeline(run);
    expect(timeline.origin).toBe(T0);
    expect(timeline.duration).toBe(5314);
    expect(timeline.steps.map((s) => [s.nodeKey, s.start, s.end])).toEqual([
      ["input", 129, 166], ["stt", 185, 1720], ["notes", 1948, 3681], ["never", null, null],
    ]);
  });

  it("lists start and finish events in time order", () => {
    const events = buildTimeline(run).events.map((e) => `${e.at} ${e.nodeKey} ${e.kind}`);
    expect(events).toEqual([
      "129 input started", "166 input finished", "185 stt started", "1720 stt finished", "1948 notes started", "3681 notes finished",
    ]);
  });

  it("ends an interrupted step where the run ended", () => {
    const timeline = buildTimeline({ ...run, node_executions: [row("cut", 100, null, "failed")] } as unknown as ExecutionDetail);
    expect(timeline.steps[0].end).toBe(5314);
  });
});

describe("stateAt", () => {
  const timeline = buildTimeline(run);

  it("is gray before a step, blue while it runs, its status after", () => {
    expect(stateAt(timeline, 0)).toEqual({ input: "idle", stt: "idle", notes: "idle", never: "idle" });
    expect(stateAt(timeline, 1000)).toMatchObject({ input: "success", stt: "running", notes: "idle" });
  });

  it("shows the hand-off gap: nothing runs between stt and notes", () => {
    expect(stateAt(timeline, 1850)).toMatchObject({ stt: "success", notes: "idle" });
    expect(stateAt(timeline, 1948)).toMatchObject({ notes: "running" });
  });

  it("marks never-run steps skipped only once the run is over", () => {
    expect(stateAt(timeline, 4000).never).toBe("idle");
    expect(stateAt(timeline, 5314).never).toBe("skipped");
  });

  it("shows failures", () => {
    const failed = buildTimeline({ ...run, node_executions: [row("x", 0, 50, "failed")] } as unknown as ExecutionDetail);
    expect(stateAt(failed, 60).x).toBe("failed");
  });
});

describe("helpers", () => {
  it("colors edges like the live canvas", () => {
    expect(edgeState("success", "running")).toBe("running");
    expect(edgeState("success", "success")).toBe("success");
    expect(edgeState("failed", "idle")).toBe("failed");
    expect(edgeState("success", "idle")).toBe("idle");
  });

  it("formats offsets", () => {
    expect(formatOffset(129)).toBe("129 ms");
    expect(formatOffset(1720)).toBe("1.7 s");
    expect(formatOffset(125_000)).toBe("2 min 05 s");
  });
});
