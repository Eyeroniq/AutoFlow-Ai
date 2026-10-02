/**
 * Run Replay: a finished run rebuilt from what was stored, nothing new recorded.
 *
 * Every node row has its started_at / finished_at and final status, and the execution keeps
 * the graph exactly as it ran. From those, a timeline: each step's start and end as offsets
 * from the run's start (so queue hand-offs and waits between steps keep their real length),
 * and the state of every node at any instant, which the canvas draws with the live run's
 * colors (gray, blue while running, green, red).
 */
import type { ExecutionDetail, NodeExecutionStatus } from "@/lib/types";

export type ReplayStatus = "idle" | "running" | "success" | "failed" | "skipped";

export interface ReplayStep {
  nodeKey: string;
  label: string;
  nodeType: string;
  /** The row's final status. */
  status: NodeExecutionStatus;
  /** Milliseconds from the run's start; null when the node never started (skipped / not run). */
  start: number | null;
  end: number | null;
  error: string | null;
}

export interface ReplayEvent {
  at: number;
  nodeKey: string;
  label: string;
  kind: "started" | "finished";
  status: ReplayStatus;
}

export interface Timeline {
  /** The run's real start (epoch ms): offset 0. */
  origin: number;
  /** Offset of the last thing that happened (at least 1 ms). */
  duration: number;
  steps: ReplayStep[];
  events: ReplayEvent[];
}

const time = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN);

export function buildTimeline(execution: Pick<ExecutionDetail, "started_at" | "finished_at" | "node_executions">): Timeline {
  const rows = execution.node_executions;
  const starts = rows.map((r) => time(r.started_at)).filter((t) => !Number.isNaN(t));
  const runStart = time(execution.started_at);
  const origin = Math.min(...(Number.isNaN(runStart) ? [] : [runStart]), ...starts, Number.POSITIVE_INFINITY);
  const base = Number.isFinite(origin) ? origin : 0;
  const offset = (iso: string | null | undefined) => {
    const t = time(iso);
    return Number.isNaN(t) ? null : Math.max(0, t - base);
  };
  const steps: ReplayStep[] = rows
    .map((row) => {
      const start = offset(row.started_at);
      // A node that started but has no finish time (an interrupted run) ends where the run ended.
      const end = start === null ? null : (offset(row.finished_at) ?? offset(execution.finished_at) ?? start);
      return {
        nodeKey: row.node_key,
        label: row.node_label,
        nodeType: row.node_type,
        status: row.status,
        start,
        end: end === null ? null : Math.max(end, start ?? 0),
        error: row.error_message,
      };
    })
    .sort((a, b) => (a.start ?? Infinity) - (b.start ?? Infinity) || (a.end ?? Infinity) - (b.end ?? Infinity));
  const ends = steps.flatMap((s) => (s.end === null ? [] : [s.end]));
  const runEnd = offset(execution.finished_at);
  const duration = Math.max(1, ...ends, ...(runEnd === null ? [] : [runEnd]));
  const events: ReplayEvent[] = [];
  for (const step of steps) {
    if (step.start === null) continue;
    events.push({ at: step.start, nodeKey: step.nodeKey, label: step.label, kind: "started", status: "running" });
    events.push({ at: step.end ?? step.start, nodeKey: step.nodeKey, label: step.label, kind: "finished", status: finalStatus(step) });
  }
  events.sort((a, b) => a.at - b.at || (a.kind === b.kind ? 0 : a.kind === "finished" ? -1 : 1));
  return { origin: base, duration, steps, events };
}

function finalStatus(step: ReplayStep): ReplayStatus {
  if (step.status === "success" || step.status === "failed" || step.status === "skipped") return step.status;
  return "idle";
}

/** Every node's state `at` ms into the run. Nodes that never ran turn "skipped" when the run ends. */
export function stateAt(timeline: Timeline, at: number): Record<string, ReplayStatus> {
  const states: Record<string, ReplayStatus> = {};
  for (const step of timeline.steps) {
    if (step.start === null) {
      states[step.nodeKey] = at >= timeline.duration && step.status === "skipped" ? "skipped" : "idle";
    } else if (at < step.start) {
      states[step.nodeKey] = "idle";
    } else if (at < (step.end ?? step.start)) {
      states[step.nodeKey] = "running";
    } else {
      states[step.nodeKey] = finalStatus(step);
    }
  }
  return states;
}

/** The edge colors the live canvas uses, from its two ends' states. */
export function edgeState(source: ReplayStatus | undefined, target: ReplayStatus | undefined): "idle" | "running" | "success" | "failed" {
  if (target === "running") return "running";
  if (target === "failed" || source === "failed") return "failed";
  if (target === "success" && source === "success") return "success";
  return "idle";
}

/** "1.2 s" / "350 ms" / "2 min 05 s" for the clock and the event list. */
export function formatOffset(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes} min ${String(Math.round((ms % 60_000) / 1000)).padStart(2, "0")} s`;
}

export const SPEEDS = [0.5, 1, 2, 5, 10] as const;
