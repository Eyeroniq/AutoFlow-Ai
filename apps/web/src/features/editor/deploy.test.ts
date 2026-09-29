import { describe, expect, it } from "vitest";

import { curlExample, exampleInputs, graphIo, shellQuote } from "./deploy";
import { fromApiGraph } from "./graph";
import { catalog } from "./test-fixtures";

const byType = Object.fromEntries(catalog.map((c) => [c.type, c]));

const flow = () =>
  fromApiGraph(
    {
      nodes: [
        { id: "out", type: "output", label: "Result", position: { x: 600, y: 0 }, config: { name: "result", value: "{{text.text}}" } },
        { id: "text", type: "text", position: { x: 300, y: 0 }, config: { text: "Hi {{input.topic}}" } },
        { id: "input", type: "input", label: "Topic", position: { x: 0, y: 0 }, config: { name: "topic", input_type: "text" } },
        { id: "n", type: "input", position: { x: 0, y: 100 }, config: { name: "count", input_type: "number", default: 3 } },
        { id: "opt", type: "input", position: { x: 0, y: 200 }, config: { input_type: "json", required: false } },
      ],
      edges: [
        { source: "input", target: "text" },
        { source: "n", target: "text" },
        { source: "opt", target: "text" },
        { source: "text", target: "out" },
      ],
      variables: [],
    },
    byType,
  );

describe("graphIo", () => {
  it("lists Input and Output nodes in execution order, like the API", () => {
    const { nodes, edges } = flow();
    const { inputs, outputs } = graphIo(nodes, edges);
    expect(inputs).toEqual([
      { node_id: "input", label: "Topic", name: "topic", type: "text", required: true, default: null },
      // No label: the catalog's; no name: the node id. A default makes it optional.
      { node_id: "n", label: "Input", name: "count", type: "number", required: false, default: 3 },
      { node_id: "opt", label: "Input", name: "opt", type: "json", required: false, default: null },
    ]);
    expect(outputs).toEqual([{ node_id: "out", label: "Result", name: "result" }]);
  });
});

describe("curl example", () => {
  const inputs = graphIo(flow().nodes, flow().edges).inputs;

  it("fills inputs with their defaults or a placeholder of their type", () => {
    expect(exampleInputs(inputs)).toEqual({ topic: "…", count: 3, opt: {} });
  });

  it("quotes for the shell so nothing in the values is expanded", () => {
    expect(shellQuote("it's $HOME")).toBe(`'it'\\''s $HOME'`);
  });

  it("uses the real key when it's known", () => {
    const command = curlExample({
      apiUrl: "http://localhost:8000",
      endpoint: "/api/v1/deployments/abc/run",
      apiKey: "ffk_secret",
      inputs: { topic: "don't panic" },
    });
    expect(command).toBe(
      [
        `curl -X POST 'http://localhost:8000/api/v1/deployments/abc/run?wait=true' \\`,
        `  -H 'Authorization: Bearer ffk_secret' \\`,
        `  -H 'Content-Type: application/json' \\`,
        `  -d '{"inputs":{"topic":"don'\\''t panic"}}'`,
      ].join("\n"),
    );
  });

  it("reads $FLOWFORGE_API_KEY otherwise, and can leave out ?wait=true", () => {
    const command = curlExample({ apiUrl: "https://api.example.com", endpoint: "/api/v1/deployments/abc/run", apiKey: null, inputs: {}, wait: false });
    expect(command).toContain(`curl -X POST 'https://api.example.com/api/v1/deployments/abc/run' \\`);
    expect(command).toContain(`-H "Authorization: Bearer $FLOWFORGE_API_KEY"`);
    expect(command).toContain(`-d '{"inputs":{}}'`);
  });
});
