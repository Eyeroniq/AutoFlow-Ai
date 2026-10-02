import { describe, expect, it } from "vitest";

import type { ExecutionDetail, ExecutionEvent } from "@/lib/types";

import { edgeRunStatus } from "../editor/graph";
import { fromExecution, idleRun, orderedNodes, reduceRun, startRun } from "./run-state";

const nodes = [
  { key: "input", label: "Topic", type: "input" },
  { key: "gemini", label: "Summarize", type: "gemini" },
  { key: "out", label: "Result", type: "output" },
];

function apply(events: ExecutionEvent[], state = startRun("ex-1", nodes)) {
  return events.reduce(reduceRun, state);
}

const t = (s: number) => `2026-09-28T00:00:0${s}Z`;

describe("reduceRun", () => {
  it("follows a run from start to finish", () => {
    const state = apply([
      { type: "execution.started", seq: 1, status: "running", started_at: t(0), worker: "worker@a" },
      { type: "node.started", seq: 2, node_key: "input", started_at: t(0) },
      { type: "node.succeeded", seq: 3, node_key: "input", status: "success", input: { name: "topic" }, output: { value: "x" }, started_at: t(0), finished_at: t(1), duration_ms: 3 },
      { type: "node.started", seq: 4, node_key: "gemini", started_at: t(1) },
      { type: "node.token", seq: 5, node_key: "gemini", text: "Hel", provider: "groq" },
      { type: "node.token", seq: 6, node_key: "gemini", text: "lo", provider: "groq" },
    ]);
    expect(state.status).toBe("running");
    expect(state.worker).toBe("worker@a");
    expect(state.nodes.input).toMatchObject({ status: "success", durationMs: 3, output: { value: "x" } });
    expect(state.nodes.gemini).toMatchObject({ status: "running", tokens: "Hello", tokenProvider: "groq" });
    expect(state.nodes.out.status).toBe("pending");

    const done = apply([
      { type: "node.succeeded", seq: 7, node_key: "gemini", status: "success", output: { response: "Hello" }, started_at: t(1), finished_at: t(2), duration_ms: 900 },
      { type: "node.skipped", seq: 8, node_key: "out", status: "skipped", reason: "Not run: Stopped by user", started_at: null, finished_at: null, duration_ms: null },
      { type: "execution.finished", seq: 9, status: "stopped", final_output: null, error: "Stopped by user", started_at: t(0), finished_at: t(3), duration_ms: 3000 },
    ], state);
    expect(done.nodes.gemini).toMatchObject({ status: "success", output: { response: "Hello" }, input: null });
    expect(done.nodes.out).toMatchObject({ status: "skipped", error: "Not run: Stopped by user" });
    expect(done).toMatchObject({ status: "stopped", error: "Stopped by user", durationMs: 3000, seq: 9 });
  });

  it("records node failures", () => {
    const state = apply([
      { type: "node.failed", seq: 1, node_key: "gemini", status: "failed", error: "gemini: rate limited (HTTP 429)", input: { user_prompt: "x" }, started_at: t(0), finished_at: t(1), duration_ms: 10 },
    ]);
    expect(state.nodes.gemini).toMatchObject({ status: "failed", error: "gemini: rate limited (HTTP 429)", input: { user_prompt: "x" } });
  });

  it("ignores duplicate or out-of-order events, and heartbeats", () => {
    const once = apply([{ type: "node.started", seq: 5, node_key: "gemini", started_at: t(1) }]);
    const again = apply([
      { type: "node.succeeded", seq: 4, node_key: "gemini", status: "success", output: {}, started_at: t(1), finished_at: t(2), duration_ms: 1 },
      { type: "heartbeat", timestamp: t(3) },
    ], once);
    expect(again.nodes.gemini.status).toBe("running");
    expect(again.seq).toBe(5);
  });

  it("restarts the token stream when the fallback chain switches provider", () => {
    const state = apply([
      { type: "node.token", seq: 1, node_key: "gemini", text: "partial from groq", provider: "groq" },
      { type: "node.token", seq: 2, node_key: "gemini", text: "Fresh", provider: "gemini" },
    ]);
    expect(state.nodes.gemini).toMatchObject({ tokens: "Fresh", tokenProvider: "gemini" });
  });

  it("replaces everything with a snapshot (late join / reconnect)", () => {
    const execution: ExecutionDetail = {
      id: "ex-1", workflow_id: "wf", status: "running", trigger: "manual", trigger_id: null,
      triggered_by_user_id: null, created_at: t(0),
      started_at: t(0), finished_at: null, error_message: null, queue: "default", worker_hostname: "worker@a",
      heartbeat_at: null, stop_requested_at: null, duration_ms: null, inputs: {}, final_output: null, segment: 0, handoff_at: null, deployment_id: null,
      node_executions: [
        { id: "1", node_id: null, node_key: "input", position: 0, node_type: "input", node_label: "Topic", status: "success", input: null, output: { value: 1 }, error_message: null, started_at: t(0), finished_at: t(1), duration_ms: 1, queue: "llm", worker_hostname: "worker-llm@a", privacy: null },
        { id: "2", node_id: null, node_key: "gemini", position: 1, node_type: "gemini", node_label: "Summarize", status: "running", input: null, output: null, error_message: null, started_at: t(1), finished_at: null, duration_ms: null, queue: "llm", worker_hostname: "worker-llm@a", privacy: null },
      ],
    };
    const streaming = apply([{ type: "node.token", seq: 3, node_key: "gemini", text: "so far", provider: "gemini" }]);
    const state = reduceRun(streaming, { type: "snapshot", seq: 4, resync: true, execution });
    expect(state.nodes.input.status).toBe("success");
    expect(state.nodes.gemini).toMatchObject({ status: "running", tokens: "so far" }); // streamed text kept
    expect(state.nodes.out).toBeUndefined(); // the snapshot is authoritative
    expect(state.seq).toBe(4);
    // Later events still apply; older ones don't.
    expect(reduceRun(state, { type: "node.started", seq: 4, node_key: "gemini", started_at: t(9) }).nodes.gemini.startedAt).toBe(t(1));
  });
});

describe("queue hand-offs", () => {
  it("records where each node ran and when the run waits for another queue", () => {
    const state = apply([
      { type: "node.started", seq: 1, node_key: "input", started_at: t(0), queue: "ocr", worker: "worker-ocr@1" },
      { type: "execution.handoff", seq: 2, from_queue: "ocr", to_queue: "llm", segment: 1, worker: "worker-ocr@1" },
    ]);
    expect(state.nodes.input).toMatchObject({ queue: "ocr", worker: "worker-ocr@1" });
    expect(state).toMatchObject({ waitingForQueue: "llm", segment: 1, worker: null });

    const resumed = reduceRun(state, { type: "execution.resumed", seq: 3, segment: 1, worker: "worker-llm@2", queue: "llm" });
    expect(resumed).toMatchObject({ waitingForQueue: null, worker: "worker-llm@2" });
    const started = reduceRun(state, { type: "node.started", seq: 3, node_key: "gemini", started_at: t(2), queue: "llm", worker: "worker-llm@2" });
    expect(started.waitingForQueue).toBeNull();
    expect(started.nodes.gemini).toMatchObject({ status: "running", queue: "llm", worker: "worker-llm@2" });
  });
});

describe("helpers", () => {
  it("orders nodes by start time, then by position", () => {
    const state = apply([
      { type: "node.started", seq: 1, node_key: "gemini", started_at: t(2) },
      { type: "node.started", seq: 2, node_key: "input", started_at: t(1) },
    ]);
    expect(orderedNodes(state).map((n) => n.key)).toEqual(["input", "gemini", "out"]);
  });

  it("colors edges by the nodes on either end", () => {
    expect(edgeRunStatus(undefined, undefined)).toBe("idle");
    expect(edgeRunStatus("success", "running")).toBe("running");
    expect(edgeRunStatus("success", "success")).toBe("success");
    expect(edgeRunStatus("success", "failed")).toBe("failed");
    expect(edgeRunStatus("failed", "skipped")).toBe("failed");
    expect(edgeRunStatus("success", "skipped")).toBe("idle");
    expect(edgeRunStatus("pending", "pending")).toBe("idle");
  });

  it("fromExecution works on a finished execution", () => {
    expect(fromExecution({
      id: "e", workflow_id: "w", status: "success", trigger: "manual", trigger_id: null,
      triggered_by_user_id: null, created_at: t(0),
      started_at: t(0),
      finished_at: t(1), error_message: null, queue: null, worker_hostname: null, heartbeat_at: null, stop_requested_at: null,
      duration_ms: 1000, inputs: null, final_output: { result: 1 }, node_executions: [], segment: 0, handoff_at: null, deployment_id: null,
    }, idleRun)).toMatchObject({ executionId: "e", status: "success", finalOutput: { result: 1 }, durationMs: 1000 });
  });
});
