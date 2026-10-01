import { describe, expect, it } from "vitest";

import { fromApiGraph } from "./graph";
import {
  buildReferenceSuggestions,
  filterSuggestions,
  findOpenReference,
  insertReference,
  referencesIn,
} from "./references";
import { catalog, workflow } from "./test-fixtures";

const byType = Object.fromEntries(catalog.map((c) => [c.type, c]));
const graph = fromApiGraph(workflow().graph, byType);
const suggest = (nodeId: string) =>
  buildReferenceSuggestions({ nodeId, nodes: graph.nodes, edges: graph.edges, variables: graph.variables, catalog: byType });

describe("buildReferenceSuggestions", () => {
  it("offers upstream outputs, Input values by name, variables, and system values", () => {
    const refs = suggest("gmail").map((s) => s.ref);
    expect(refs).toEqual([
      "input.topic",
      "input.value",
      "gemini.fallback_errors",
      "gemini.mock",
      "gemini.model",
      "gemini.provider",
      "gemini.provider_used",
      "gemini.response",
      "gemini",
      "vars.recipient",
      "system.execution_id",
      "system.workflow_id",
      "system.node_id",
      "system.now",
      "system.today",
    ]);
  });

  it("only includes nodes that are upstream (not itself, not downstream)", () => {
    const refs = suggest("gemini").map((s) => s.ref);
    expect(refs).toContain("input.topic");
    expect(refs.some((r) => r.startsWith("gemini"))).toBe(false);
    expect(refs.some((r) => r.startsWith("gmail"))).toBe(false);
    expect(suggest("input").filter((s) => s.kind === "node" || s.kind === "input")).toEqual([]);
  });

  it("labels suggestions by source", () => {
    const [topic] = suggest("gemini");
    expect(topic).toEqual({ ref: "input.topic", kind: "input", detail: "Topic · input" });
    expect(suggest("gmail").find((s) => s.ref === "gemini.response")).toEqual({
      ref: "gemini.response", kind: "node", detail: "Summarize · output",
    });
  });

  it("skips invalid or duplicate variable keys", () => {
    const refs = buildReferenceSuggestions({
      nodeId: "gemini", nodes: graph.nodes, edges: graph.edges, catalog: byType,
      variables: [
        { key: "ok", value: "1", type: "workflow" },
        { key: "ok", value: "2", type: "workflow" },
        { key: "has space", value: "3", type: "workflow" },
      ],
    }).filter((s) => s.kind === "variable");
    expect(refs.map((s) => s.ref)).toEqual(["vars.ok"]);
  });
});

describe("findOpenReference", () => {
  it("detects an unclosed {{ before the caret", () => {
    expect(findOpenReference("Hello {{", 8)).toEqual({ start: 6, query: "" });
    expect(findOpenReference("Hello {{gem", 11)).toEqual({ start: 6, query: "gem" });
    expect(findOpenReference("Hi {{ input.to", 14)).toEqual({ start: 3, query: "input.to" });
  });

  it("ignores closed references and plain braces", () => {
    expect(findOpenReference("Hello {{name}} there", 20)).toBeNull();
    expect(findOpenReference("{ not a ref", 11)).toBeNull();
    expect(findOpenReference("no braces", 9)).toBeNull();
    expect(findOpenReference("{{a\nb", 5)).toBeNull();
  });

  it("uses the caret, not the end of the text", () => {
    expect(findOpenReference("{{gem}} and more", 5)).toEqual({ start: 0, query: "gem" });
  });
});

describe("filterSuggestions", () => {
  const all = suggest("gmail");

  it("returns everything for an empty query", () => {
    expect(filterSuggestions(all, "")).toHaveLength(all.length);
  });

  it("ranks prefix matches first, then segment prefixes, then substrings", () => {
    expect(filterSuggestions(all, "gem").map((s) => s.ref).slice(0, 2)).toEqual(["gemini.fallback_errors", "gemini.mock"]);
    const resp = filterSuggestions(all, "resp").map((s) => s.ref);
    expect(resp[0]).toBe("gemini.response");
    const vars = filterSuggestions(all, "recip").map((s) => s.ref);
    expect(vars).toEqual(["vars.recipient"]);
    expect(filterSuggestions(all, "gemini.response").map((s) => s.ref)[0]).toBe("gemini.response");
  });

  it("is case-insensitive and limited", () => {
    expect(filterSuggestions(all, "VARS").map((s) => s.ref)).toEqual(["vars.recipient"]);
    expect(filterSuggestions(all, "", 3)).toHaveLength(3);
    expect(filterSuggestions(all, "zzz")).toEqual([]);
  });
});

describe("insertReference", () => {
  it("replaces the typed part and places the caret after the reference", () => {
    const text = "Summary: {{gem";
    const open = findOpenReference(text, text.length)!;
    expect(insertReference(text, text.length, open.start, "gemini.response")).toEqual({
      text: "Summary: {{gemini.response}}",
      caret: "Summary: {{gemini.response}}".length,
    });
  });

  it("keeps text after the caret and swallows auto-closed braces", () => {
    const text = "A {{in}} B";
    const result = insertReference(text, 6, 2, "input.topic");
    expect(result.text).toBe("A {{input.topic}} B");
    expect(result.caret).toBe("A {{input.topic}}".length);
    expect(insertReference("x {{ tail", 4, 2, "vars.a").text).toBe("x {{vars.a}} tail");
  });
});

describe("referencesIn", () => {
  it("lists references in a string", () => {
    expect(referencesIn("Hi {{ vars.name }}, see {{gemini.response}}")).toEqual(["vars.name", "gemini.response"]);
    expect(referencesIn("none")).toEqual([]);
  });
});
