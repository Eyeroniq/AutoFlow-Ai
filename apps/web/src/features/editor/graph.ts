import type { Edge, Node } from "@xyflow/react";

import type {
  ApiGraphEdge,
  ApiGraphNode,
  NodeExecutionStatus,
  NodeType,
  WorkflowGraph,
  WorkflowVariable,
} from "@/lib/types";

export interface FlowNodeData extends Record<string, unknown> {
  nodeType: string;
  label: string;
  description: string;
  config: Record<string, unknown>;
  collapsed: boolean;
}

export type FlowNode = Node<FlowNodeData, "flow">;
export type FlowEdge = Edge;
export type Catalog = Record<string, NodeType>;

/** Graph ids appear inside {{...}} references (engine: IDENTIFIER_PATTERN). */
export const IDENTIFIER = /^[A-Za-z0-9_-]+$/;
export const RESERVED_IDS = new Set(["vars", "system"]);

export function edgeId(source: string, sourceHandle: string | null | undefined, target: string): string {
  return `${source}${sourceHandle ? `:${sourceHandle}` : ""}->${target}`;
}

export function toFlowNode(node: ApiGraphNode, catalog: Catalog): FlowNode {
  const entry = catalog[node.type];
  return {
    id: node.id,
    type: "flow",
    position: { x: node.position?.x ?? 0, y: node.position?.y ?? 0 },
    data: {
      nodeType: node.type,
      label: node.label || entry?.label || node.type,
      description: node.description ?? "",
      config: { ...(node.config ?? {}) },
      collapsed: Boolean(node.collapsed),
    },
  };
}

export function toFlowEdge(edge: ApiGraphEdge): FlowEdge {
  return {
    id: edge.id || edgeId(edge.source, edge.source_handle, edge.target),
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.source_handle ?? null,
    targetHandle: edge.target_handle ?? null,
  };
}

export function fromApiGraph(graph: WorkflowGraph, catalog: Catalog) {
  return {
    nodes: (graph.nodes ?? []).map((n) => toFlowNode(n, catalog)),
    edges: (graph.edges ?? []).map(toFlowEdge),
    variables: (graph.variables ?? []).map((v) => ({ key: v.key, value: v.value ?? "", type: v.type ?? "workflow" })),
  };
}

export function toApiGraph(nodes: FlowNode[], edges: FlowEdge[], variables: WorkflowVariable[]): WorkflowGraph {
  return {
    nodes: nodes.map((n) => ({
      id: n.id,
      type: n.data.nodeType,
      label: n.data.label,
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      config: n.data.config,
      description: n.data.description || null,
      collapsed: n.data.collapsed || undefined,
    })),
    edges: edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      source_handle: e.sourceHandle ?? null,
      target_handle: e.targetHandle ?? null,
    })),
    variables: variables.map((v) => ({ key: v.key, value: v.value, type: v.type })),
  };
}

/** A readable, reference-safe id: "gemini", "gemini_2", ... (never "vars"/"system"). */
export function uniqueNodeId(base: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const clean = base.replace(/[^A-Za-z0-9_-]/g, "_") || "node";
  const stem = RESERVED_IDS.has(clean) ? `${clean}_node` : clean;
  if (!used.has(stem)) return stem;
  for (let i = 2; ; i += 1) {
    const candidate = `${stem}_${i}`;
    if (!used.has(candidate)) return candidate;
  }
}

const CARD_WIDTH = 240;
const CARD_HEIGHT = 96;
const CARD_GAP = 32;

type Placed = Pick<FlowNode, "position" | "measured">;

const sizeOf = (n: Placed) => ({ width: n.measured?.width ?? CARD_WIDTH, height: n.measured?.height ?? CARD_HEIGHT });

/** Whether a card of `size` at `at` would touch (within CARD_GAP) any of `nodes`. */
function overlapsAny(nodes: Placed[], at: { x: number; y: number }, size = { width: CARD_WIDTH, height: CARD_HEIGHT }) {
  return nodes.some((n) => {
    const { width, height } = sizeOf(n);
    return (
      at.x < n.position.x + width + CARD_GAP &&
      n.position.x < at.x + size.width + CARD_GAP &&
      at.y < n.position.y + height + CARD_GAP &&
      n.position.y < at.y + size.height + CARD_GAP
    );
  });
}

/** The first spot at or below `position` where a new card doesn't cover an existing one. */
export function freePosition(nodes: Placed[], position: { x: number; y: number }) {
  let y = position.y;
  for (let step = 0; step < 50 && overlapsAny(nodes, { x: position.x, y }); step += 1) y += CARD_HEIGHT / 2;
  return { x: position.x, y };
}

/** How far to move copies of `originals` so they sit below them without covering any node. */
export function duplicateOffset(originals: Placed[], nodes: Placed[]) {
  const top = Math.min(...originals.map((n) => n.position.y));
  const bottom = Math.max(...originals.map((n) => n.position.y + sizeOf(n).height));
  let dy = bottom - top + CARD_GAP;
  const clashes = () => originals.some((n) => overlapsAny(nodes, { x: n.position.x, y: n.position.y + dy }, sizeOf(n)));
  for (let step = 0; step < 50 && clashes(); step += 1) dy += CARD_HEIGHT / 2;
  return { x: 0, y: dy };
}

/** Starting config for a new node. Schema defaults fill in the rest on the server. */
export function defaultConfig(type: string, id: string, nodes: FlowNode[]): Record<string, unknown> {
  if (type === "input") return { name: id, input_type: "text" };
  if (type === "output") {
    const names = new Set(nodes.filter((n) => n.data.nodeType === "output").map((n) => String(n.data.config.name ?? "result")));
    return { name: names.has("result") ? id : "result", value: "" };
  }
  return {};
}

export function ancestorsOf(nodeId: string, edges: Pick<FlowEdge, "source" | "target">[]): Set<string> {
  const parents = new Map<string, string[]>();
  for (const edge of edges) parents.set(edge.target, [...(parents.get(edge.target) ?? []), edge.source]);
  const seen = new Set<string>();
  const stack = [...(parents.get(nodeId) ?? [])];
  while (stack.length) {
    const current = stack.pop()!;
    if (current === nodeId || seen.has(current)) continue;
    seen.add(current);
    stack.push(...(parents.get(current) ?? []));
  }
  return seen;
}

/** Top-level output keys a node exposes to {{node.key}} references. */
export function outputKeysFor(node: Pick<FlowNode, "id" | "data">, catalog: Catalog): string[] {
  if (node.data.nodeType === "input") {
    const name = typeof node.data.config.name === "string" && node.data.config.name ? node.data.config.name : node.id;
    return name === "value" ? ["value"] : [name, "value"];
  }
  return catalog[node.data.nodeType]?.output_keys ?? [];
}

function preview(value: unknown, limit = 48): string {
  if (value === undefined || value === null || value === "") return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
}

/** The one-line config summary on a node card. */
export function configSummary(type: string, config: Record<string, unknown>, entry?: NodeType): string {
  const c = config;
  const str = (key: string) => (c[key] === undefined || c[key] === null ? "" : String(c[key]));
  switch (type) {
    case "input": {
      const hasDefault = c.default !== undefined && c.default !== null && c.default !== "";
      const shown = c.input_type === "file" ? "a default file" : preview(c.default, 24);
      return `${str("name") || "value"} · ${str("input_type") || "text"}${hasDefault ? ` = ${shown}` : ""}`;
    }
    case "pdf_extract":
      return `${preview(c.file, 28) || "(no file)"} · pages ${str("pages") || "all"}`;
    case "ocr":
      return `${preview(c.file, 22) || "(no file)"} · ${str("language") || "eng"} · ${str("dpi") || "300"} dpi`;
    case "summarize":
      return `${str("length") || "medium"} · ${str("style") || "paragraph"} · ${str("provider") || "gemini"}`;
    case "extract_entities": {
      const types = Array.isArray(c.entity_types) ? c.entity_types.length : 4;
      const custom = Array.isArray(c.custom_types) && c.custom_types.length ? ` + ${c.custom_types.length} custom` : "";
      return `${types} types${custom} · ${str("provider") || "gemini"}`;
    }
    case "output":
      return `${str("name") || "result"} ← ${preview(c.value) || "(empty)"}`;
    case "text":
      return preview(c.text) || "(empty)";
    case "condition":
      return `${preview(c.left, 16) || "?"} ${str("operator").replace(/_/g, " ") || "?"} ${preview(c.right, 16) || "?"}`;
    case "delay":
      return `wait ${str("seconds") || "?"}s`;
    case "http_request":
      return `${str("method") || "GET"} ${preview(c.url) || "(no url)"}`;
    case "gmail":
      return `to ${preview(c.to, 36) || "(no recipient)"}${c.auth === "mock" ? " · mock" : ""}`;
    case "gmail_read":
      return `${str("folder") || "INBOX"} · ${c.unread_only === false ? "all" : "unread"} · max ${str("max_results") || "10"}`;
    default: {
      if (entry?.category === "ai") {
        const provider = str("provider") || String(entry.config_schema.properties?.provider?.default ?? type);
        const fallback = Array.isArray(c.fallback) && c.fallback.length ? ` → ${(c.fallback as string[]).join(" → ")}` : "";
        return `${provider} · ${str("model") || "default model"}${c.stream ? " · stream" : ""}${fallback}`;
      }
      const first = Object.values(c).find((v) => typeof v === "string" && v);
      return preview(first);
    }
  }
}

export type EdgeRunStatus = "idle" | "running" | "success" | "failed";

/** Edge color during/after a run: blue while its target runs, green/red once it's done. */
export function edgeRunStatus(source?: NodeExecutionStatus, target?: NodeExecutionStatus): EdgeRunStatus {
  if (target === "running") return "running";
  if (target === "failed" || source === "failed") return "failed";
  if (target === "success" && source === "success") return "success";
  return "idle";
}

/** Node ids in execution order (Kahn; ties and cycles fall back to declaration order). */
export function topologicalOrder(nodes: Pick<FlowNode, "id">[], edges: Pick<FlowEdge, "source" | "target">[]): string[] {
  const ids = nodes.map((n) => n.id);
  const known = new Set(ids);
  const indegree = new Map(ids.map((id) => [id, 0]));
  const children = new Map<string, string[]>();
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) continue;
    indegree.set(e.target, (indegree.get(e.target) ?? 0) + 1);
    children.set(e.source, [...(children.get(e.source) ?? []), e.target]);
  }
  const order: string[] = [];
  const ready = ids.filter((id) => indegree.get(id) === 0);
  while (ready.length) {
    const id = ready.shift()!;
    order.push(id);
    for (const child of children.get(id) ?? []) {
      indegree.set(child, indegree.get(child)! - 1);
      if (indegree.get(child) === 0) ready.push(child);
    }
  }
  return [...order, ...ids.filter((id) => !order.includes(id))];
}
