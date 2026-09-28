import { describe, expect, it } from "vitest";

import { ancestorsOf, configSummary, freePosition, fromApiGraph, outputKeysFor, toApiGraph, topologicalOrder, uniqueNodeId } from "./graph";
import { catalog, workflow } from "./test-fixtures";

const byType = Object.fromEntries(catalog.map((c) => [c.type, c]));

describe("graph conversion", () => {
  it("round-trips the API graph", () => {
    const original = workflow().graph;
    const flow = fromApiGraph(original, byType);
    const back = toApiGraph(flow.nodes, flow.edges, flow.variables);
    expect(back.nodes.map((n) => ({ id: n.id, type: n.type, label: n.label, config: n.config, position: n.position }))).toEqual(
      original.nodes.map((n) => ({ id: n.id, type: n.type, label: n.label, config: n.config, position: n.position })),
    );
    expect(back.edges.map((e) => [e.source, e.target])).toEqual([["input", "gemini"], ["gemini", "gmail"]]);
    expect(back.variables).toEqual(original.variables);
  });

  it("maps handles to snake_case and back", () => {
    const flow = fromApiGraph({ nodes: [], edges: [{ source: "c", target: "x", source_handle: "true" }], variables: [] }, byType);
    expect(flow.edges[0]).toMatchObject({ id: "c:true->x", sourceHandle: "true" });
    expect(toApiGraph([], flow.edges, []).edges[0]).toMatchObject({ source_handle: "true", target_handle: null });
  });
});

describe("helpers", () => {
  it("makes reference-safe unique ids", () => {
    expect(uniqueNodeId("gemini", [])).toBe("gemini");
    expect(uniqueNodeId("gemini", ["gemini", "gemini_2"])).toBe("gemini_3");
    expect(uniqueNodeId("vars", [])).toBe("vars_node");
    expect(uniqueNodeId("http request", [])).toBe("http_request");
  });

  it("finds ancestors through the edges", () => {
    const edges = [{ source: "a", target: "b" }, { source: "b", target: "c" }, { source: "x", target: "c" }];
    expect([...ancestorsOf("c", edges)].sort()).toEqual(["a", "b", "x"]);
    expect([...ancestorsOf("a", edges)]).toEqual([]);
    expect([...ancestorsOf("a", [{ source: "a", target: "b" }, { source: "b", target: "a" }])]).toEqual(["b"]); // cycles terminate
  });

  it("derives output keys (Input nodes by their name)", () => {
    const [input, gemini] = fromApiGraph(workflow().graph, byType).nodes;
    expect(outputKeysFor(input, byType)).toEqual(["topic", "value"]);
    expect(outputKeysFor(gemini, byType)).toContain("response");
  });

  it("summarizes configs on one line", () => {
    expect(configSummary("gemini", { provider: "groq", model: "gpt-oss", fallback: ["gemini"] }, byType.gemini)).toBe("groq · gpt-oss → gemini");
    expect(configSummary("gemini", {}, byType.gemini)).toBe("gemini · default model");
    expect(configSummary("delay", { seconds: 5 })).toBe("wait 5s");
    expect(configSummary("input", { name: "topic", input_type: "text" })).toBe("topic · text");
    expect(configSummary("gmail", { to: "{{vars.recipient}}" })).toBe("to {{vars.recipient}}");
    expect(configSummary("http_request", { url: "https://example.com" })).toBe("GET https://example.com");
  });
});

describe("topologicalOrder", () => {
  it("orders by dependencies and keeps cyclic nodes at the end", () => {
    const nodes = [{ id: "c" }, { id: "a" }, { id: "b" }, { id: "x" }, { id: "y" }];
    const edges = [{ source: "a", target: "b" }, { source: "b", target: "c" }, { source: "x", target: "y" }, { source: "y", target: "x" }];
    expect(topologicalOrder(nodes, edges)).toEqual(["a", "b", "c", "x", "y"]);
  });
});

describe("freePosition", () => {
  it("keeps a free spot and moves down past cards it would cover", () => {
    const nodes = [{ position: { x: 0, y: 0 } }, { position: { x: 0, y: 150 }, measured: { width: 240, height: 120 } }];
    expect(freePosition(nodes, { x: 600, y: 0 })).toEqual({ x: 600, y: 0 });
    const spot = freePosition(nodes, { x: 40, y: 20 });
    expect(spot.x).toBe(40);
    expect(spot.y).toBeGreaterThanOrEqual(150 + 120 + 32);
  });
});
