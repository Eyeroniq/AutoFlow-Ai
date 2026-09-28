/**
 * Live run state from WebSocket messages (snapshot + events). Pure (unit tested); the editor
 * and the execution detail page both render from it.
 */
import type {
  ExecutionDetail,
  ExecutionEvent,
  ExecutionStatus,
  NodeExecution,
  NodeExecutionStatus,
} from "@/lib/types";

export interface NodeRun {
  key: string;
  label: string;
  type: string;
  position: number;
  status: NodeExecutionStatus;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  error: string | null;
  /** Streamed LLM text so far (node.token events). */
  tokens: string;
  tokenProvider: string | null;
}

export interface RunState {
  executionId: string | null;
  status: ExecutionStatus | "idle";
  nodes: Record<string, NodeRun>;
  error: string | null;
  finalOutput: Record<string, unknown> | null;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
  worker: string | null;
  /** Highest event seq applied; older/duplicate events are ignored. */
  seq: number;
}

export const idleRun: RunState = {
  executionId: null,
  status: "idle",
  nodes: {},
  error: null,
  finalOutput: null,
  startedAt: null,
  finishedAt: null,
  durationMs: null,
  worker: null,
  seq: 0,
};

export const TERMINAL: ReadonlySet<string> = new Set(["success", "failed", "stopped"]);
export const isTerminal = (status: string) => TERMINAL.has(status);

interface NodeInfo {
  key: string;
  label: string;
  type: string;
}

/** A fresh run: every node pending (in the given order) until events say otherwise. */
export function startRun(executionId: string, nodes: NodeInfo[]): RunState {
  return {
    ...idleRun,
    executionId,
    status: "pending",
    nodes: Object.fromEntries(nodes.map((n, position) => [n.key, blankNode(n, position)])),
  };
}

function blankNode(info: NodeInfo, position: number): NodeRun {
  return {
    ...info,
    position,
    status: "pending",
    startedAt: null,
    finishedAt: null,
    durationMs: null,
    input: null,
    output: null,
    error: null,
    tokens: "",
    tokenProvider: null,
  };
}

function fromExecutionNode(row: NodeExecution, index: number, previous?: NodeRun): NodeRun {
  return {
    key: row.node_key,
    label: row.node_label,
    type: row.node_type,
    position: row.position ?? index,
    status: row.status,
    startedAt: row.started_at,
    finishedAt: row.finished_at,
    durationMs: row.duration_ms,
    input: row.input,
    output: row.output,
    error: row.error_message,
    // Keep streamed text while the node is still running (the DB has no partial output).
    tokens: row.status === "running" ? (previous?.tokens ?? "") : "",
    tokenProvider: row.status === "running" ? (previous?.tokenProvider ?? null) : null,
  };
}

/** The whole state from an execution (a WS snapshot or GET /api/executions/{id}). */
export function fromExecution(execution: ExecutionDetail, previous: RunState = idleRun, seq = 0): RunState {
  const nodes: Record<string, NodeRun> = {};
  execution.node_executions.forEach((row, index) => {
    nodes[row.node_key] = fromExecutionNode(row, index, previous.nodes[row.node_key]);
  });
  return {
    executionId: execution.id,
    status: execution.status,
    nodes,
    error: execution.error_message,
    finalOutput: execution.final_output,
    startedAt: execution.started_at,
    finishedAt: execution.finished_at,
    durationMs: execution.duration_ms,
    worker: execution.worker_hostname,
    seq: Math.max(seq, previous.executionId === execution.id ? previous.seq : 0),
  };
}

function patchNode(state: RunState, key: string, patch: Partial<NodeRun>): RunState {
  const current = state.nodes[key] ?? blankNode({ key, label: key, type: "" }, Object.keys(state.nodes).length);
  return { ...state, nodes: { ...state.nodes, [key]: { ...current, ...patch } } };
}

/** Apply one message. Events at or below the last applied seq are ignored (dedupe). */
export function reduceRun(state: RunState, message: ExecutionEvent): RunState {
  if (message.type === "snapshot") return fromExecution(message.execution, state, message.seq);
  if (!("seq" in message)) return state; // heartbeat, pong, error
  if (typeof message.seq === "number") {
    if (message.seq <= state.seq) return state;
    state = { ...state, seq: message.seq };
  }
  switch (message.type) {
    case "execution.started":
      return { ...state, status: "running", startedAt: message.started_at, worker: message.worker ?? null };
    case "node.started":
      return patchNode({ ...state, status: "running" }, message.node_key, {
        status: "running",
        startedAt: message.started_at,
        finishedAt: null,
        durationMs: null,
        error: null,
        tokens: "",
        tokenProvider: null,
      });
    case "node.token": {
      const node = state.nodes[message.node_key];
      const restart = node?.tokenProvider !== null && node?.tokenProvider !== undefined && node.tokenProvider !== message.provider;
      return patchNode(state, message.node_key, {
        tokens: (restart ? "" : (node?.tokens ?? "")) + message.text,
        tokenProvider: message.provider,
      });
    }
    case "node.succeeded":
    case "node.failed":
    case "node.skipped":
      return patchNode(state, message.node_key, {
        status: message.status,
        startedAt: message.started_at ?? state.nodes[message.node_key]?.startedAt ?? null,
        finishedAt: message.finished_at,
        durationMs: message.duration_ms,
        input: message.input ?? state.nodes[message.node_key]?.input ?? null,
        output: message.output ?? null,
        error: message.type === "node.failed" ? (message.error ?? null) : message.type === "node.skipped" ? (message.reason ?? null) : null,
      });
    case "execution.finished":
      return {
        ...state,
        status: message.status,
        finalOutput: message.final_output,
        error: message.error,
        startedAt: message.started_at ?? state.startedAt,
        finishedAt: message.finished_at,
        durationMs: message.duration_ms,
      };
    default:
      return state;
  }
}

/** Nodes in run order: started ones by start time, then the rest by position. */
export function orderedNodes(state: RunState): NodeRun[] {
  return Object.values(state.nodes).sort((a, b) => {
    if (a.startedAt && b.startedAt) return a.startedAt.localeCompare(b.startedAt) || a.position - b.position;
    if (a.startedAt) return -1;
    if (b.startedAt) return 1;
    return a.position - b.position;
  });
}
