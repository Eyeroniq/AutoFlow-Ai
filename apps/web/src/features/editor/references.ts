/**
 * {{...}} reference autocomplete: which references a node can use, detecting an open "{{"
 * at the caret, ranking suggestions, and inserting one. Pure functions (unit tested).
 */
import type { WorkflowVariable } from "@/lib/types";

import { ancestorsOf, type Catalog, type FlowEdge, type FlowNode, IDENTIFIER, outputKeysFor } from "./graph";

export type ReferenceKind = "input" | "node" | "variable" | "system" | "item";

export interface ReferenceSuggestion {
  /** What goes between the braces, e.g. "gemini.response". */
  ref: string;
  kind: ReferenceKind;
  /** Secondary text, e.g. "Summarize · output". */
  detail: string;
}

export const SYSTEM_REFERENCES = ["system.execution_id", "system.workflow_id", "system.node_id", "system.now", "system.today"];

/** Offered first in a list node's per-item fields (For Each's prompt, Join's template). */
export const ITEM_REFERENCES: ReferenceSuggestion[] = [
  { ref: "item", kind: "item", detail: "the current list item" },
  { ref: "item.title", kind: "item", detail: "a field of the item" },
  { ref: "index", kind: "item", detail: "its position (0-based)" },
];

interface SuggestionContext {
  nodeId: string;
  nodes: FlowNode[];
  edges: Pick<FlowEdge, "source" | "target">[];
  variables: WorkflowVariable[];
  catalog: Catalog;
  /** The config field being edited: per-item fields also get {{item}} and {{index}}. */
  field?: string;
}

/**
 * Every reference the backend validator would accept in `nodeId`'s config: upstream
 * nodes' outputs (Input nodes as {{input.<name>}}), workflow variables, system values.
 */
export function buildReferenceSuggestions({ nodeId, nodes, edges, variables, catalog, field }: SuggestionContext): ReferenceSuggestion[] {
  const upstream = ancestorsOf(nodeId, edges);
  const self = nodes.find((n) => n.id === nodeId);
  const itemFields = (self && catalog[self.data.nodeType]?.item_fields) ?? [];
  const suggestions: ReferenceSuggestion[] = field && itemFields.includes(field) ? [...ITEM_REFERENCES] : [];
  // Declaration order is close to execution order and stable for the user.
  for (const node of nodes) {
    if (!upstream.has(node.id)) continue;
    const label = node.data.label || node.id;
    if (node.data.nodeType === "input") {
      for (const key of outputKeysFor(node, catalog)) {
        suggestions.push({ ref: `${node.id}.${key}`, kind: "input", detail: `${label} · input` });
      }
      continue;
    }
    for (const key of outputKeysFor(node, catalog)) {
      suggestions.push({ ref: `${node.id}.${key}`, kind: "node", detail: `${label} · output` });
    }
    suggestions.push({ ref: node.id, kind: "node", detail: `${label} · whole output` });
  }
  const seenVars = new Set<string>();
  for (const variable of variables) {
    if (!IDENTIFIER.test(variable.key) || seenVars.has(variable.key)) continue;
    seenVars.add(variable.key);
    suggestions.push({ ref: `vars.${variable.key}`, kind: "variable", detail: "workflow variable" });
  }
  for (const ref of SYSTEM_REFERENCES) suggestions.push({ ref, kind: "system", detail: "system value" });
  return suggestions;
}

/**
 * If the caret sits inside an unclosed "{{", where it starts and what has been typed so
 * far ("{{gem|" -> {start, query: "gem"}); otherwise null.
 */
export function findOpenReference(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const start = before.lastIndexOf("{{");
  if (start === -1) return null;
  const typed = before.slice(start + 2);
  if (typed.includes("}") || typed.includes("{") || typed.includes("\n")) return null;
  if (!/^[\sA-Za-z0-9_.\-[\]]*$/.test(typed)) return null;
  return { start, query: typed.trim() };
}

/** Rank suggestions for what's been typed: prefix of the ref, then of a segment, then substring. */
export function filterSuggestions(suggestions: ReferenceSuggestion[], query: string, limit = 50): ReferenceSuggestion[] {
  const q = query.toLowerCase();
  if (!q) return suggestions.slice(0, limit);
  const scored: { s: ReferenceSuggestion; score: number; index: number }[] = [];
  suggestions.forEach((s, index) => {
    const ref = s.ref.toLowerCase();
    let score = -1;
    if (ref === q) score = 0;
    else if (ref.startsWith(q)) score = 1;
    else if (ref.split(".").some((segment) => segment.startsWith(q))) score = 2;
    else if (ref.includes(q)) score = 3;
    if (score >= 0) scored.push({ s, score, index });
  });
  scored.sort((a, b) => a.score - b.score || a.index - b.index);
  return scored.slice(0, limit).map((x) => x.s);
}

/**
 * Replace the open reference (from `start` to the caret) with "{{ref}}"; swallows a "}}"
 * right after the caret so completing inside "{{|}}" doesn't leave extra braces.
 */
export function insertReference(text: string, caret: number, start: number, ref: string): { text: string; caret: number } {
  let end = caret;
  const after = text.slice(caret);
  const closing = after.match(/^[\sA-Za-z0-9_.\-[\]]*\}\}/);
  if (closing) end = caret + closing[0].length;
  const inserted = `{{${ref}}}`;
  return { text: text.slice(0, start) + inserted + text.slice(end), caret: start + inserted.length };
}

/** The references used in a string, e.g. "Hi {{vars.name}}" -> ["vars.name"]. */
export function referencesIn(text: string): string[] {
  return [...text.matchAll(/\{\{\s*([^{}]+?)\s*\}\}/g)].map((m) => m[1]);
}
