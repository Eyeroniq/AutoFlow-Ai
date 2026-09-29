import type { DeploymentInput, DeploymentOutput } from "@/lib/types";

import { type FlowEdge, type FlowNode, topologicalOrder } from "./graph";

/** The graph's Input and Output nodes in execution order, as the API describes a deployment
 * (apps/api/app/services/deployments.py: describe_io). */
export function graphIo(
  nodes: Pick<FlowNode, "id" | "data">[],
  edges: Pick<FlowEdge, "source" | "target">[],
): { inputs: DeploymentInput[]; outputs: DeploymentOutput[] } {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const inputs: DeploymentInput[] = [];
  const outputs: DeploymentOutput[] = [];
  for (const id of topologicalOrder(nodes, edges)) {
    const node = byId.get(id);
    if (!node) continue;
    const config = node.data.config;
    if (node.data.nodeType === "input") {
      const name = typeof config.name === "string" && config.name ? config.name : node.id;
      const fallback = config.default === undefined ? null : config.default;
      inputs.push({
        node_id: node.id,
        label: node.data.label || name,
        name,
        type: (config.input_type as DeploymentInput["type"]) || "text",
        required: config.required !== false && fallback === null,
        default: fallback,
      });
    } else if (node.data.nodeType === "output") {
      const name = typeof config.name === "string" && config.name ? config.name : "result";
      outputs.push({ node_id: node.id, label: node.data.label || name, name });
    }
  }
  return { inputs, outputs };
}

/** A request body a caller could send: each input's default, else a placeholder of its type. */
export function exampleInputs(inputs: DeploymentInput[]): Record<string, unknown> {
  const placeholder: Record<DeploymentInput["type"], unknown> = {
    text: "…",
    number: 0,
    json: {},
    file: "<file id from POST /api/files>",
  };
  return Object.fromEntries(
    inputs.map((input) => [input.name, input.default ?? placeholder[input.type] ?? placeholder.text]),
  );
}

/** Single-quoted for POSIX shells: nothing inside is expanded. */
export function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

export const API_KEY_PLACEHOLDER = "$FLOWFORGE_API_KEY";

export function curlExample({
  apiUrl,
  endpoint,
  apiKey,
  inputs,
  wait = true,
}: {
  apiUrl: string;
  endpoint: string;
  /** The real key if it's known right now; otherwise the command reads $FLOWFORGE_API_KEY. */
  apiKey: string | null;
  inputs: Record<string, unknown>;
  wait?: boolean;
}): string {
  const url = `${apiUrl}${endpoint}${wait ? "?wait=true" : ""}`;
  // The placeholder goes in double quotes so the shell substitutes the variable.
  const auth = apiKey ? shellQuote(`Authorization: Bearer ${apiKey}`) : `"Authorization: Bearer ${API_KEY_PLACEHOLDER}"`;
  return [
    `curl -X POST ${shellQuote(url)} \\`,
    `  -H ${auth} \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -d ${shellQuote(JSON.stringify({ inputs }))}`,
  ].join("\n");
}
