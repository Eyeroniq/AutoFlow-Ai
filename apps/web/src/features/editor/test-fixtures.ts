import type { NodeType, Workflow } from "@/lib/types";

/** A minimal catalog in the shape GET /api/nodes returns. */
export const catalog: NodeType[] = [
  {
    type: "input", category: "io", group: "General", label: "Input", description: "Entry point", icon: "log-in",
    queue: "default", portable: true, interruptible: true, branches: [], item_fields: [], has_input: false, produces_final_output: false,
    config_schema: { properties: { name: { type: "string" } } }, output_schema: null, output_keys: null,
  },
  {
    type: "gemini", category: "ai", group: "LLM", label: "Gemini", description: "LLM", icon: "sparkles",
    queue: "llm", portable: false, interruptible: true, branches: [], item_fields: [], has_input: true, produces_final_output: false,
    config_schema: { properties: { provider: { enum: ["gemini", "groq"], type: "string", default: "gemini" }, user_prompt: { type: "string", minLength: 1 } }, required: ["user_prompt"] },
    output_schema: null, output_keys: ["fallback_errors", "mock", "model", "provider", "provider_used", "response"],
  },
  {
    type: "gmail", category: "integration", group: "Integrations", label: "Gmail", description: "Email", icon: "mail",
    queue: "default", portable: false, interruptible: false, branches: [], item_fields: [], has_input: true, produces_final_output: false,
    config_schema: { properties: {} }, output_schema: null, output_keys: ["from", "message_id", "status"],
  },
  {
    type: "for_each", category: "lists", group: "Lists", label: "For Each", description: "Per item", icon: "repeat",
    queue: "llm", portable: false, interruptible: true, branches: [], item_fields: ["prompt", "system_prompt"], has_input: true,
    produces_final_output: false,
    config_schema: { properties: { items: {}, prompt: { type: "string", minLength: 1 } }, required: ["items", "prompt"] },
    output_schema: null, output_keys: ["count", "outputs", "results"],
  },
  {
    type: "output", category: "io", group: "General", label: "Output", description: "Result", icon: "log-out",
    queue: "default", portable: true, interruptible: true, branches: [], item_fields: [], has_input: true, produces_final_output: true,
    config_schema: { properties: {} }, output_schema: null, output_keys: ["name", "value"],
  },
];

export function workflow(overrides: Partial<Workflow> = {}): Workflow {
  return {
    id: "wf-1",
    name: "Demo",
    description: null,
    status: "draft",
    version: 3,
    created_at: "2026-09-28T00:00:00Z",
    updated_at: "2026-09-28T00:00:00Z",
    graph: {
      nodes: [
        { id: "input", type: "input", label: "Topic", position: { x: 0, y: 0 }, config: { name: "topic" } },
        { id: "gemini", type: "gemini", label: "Summarize", position: { x: 300, y: 0 }, config: { user_prompt: "About {{input.topic}}" } },
        { id: "gmail", type: "gmail", label: "Email", position: { x: 600, y: 0 }, config: { to: "{{vars.recipient}}" } },
      ],
      edges: [
        { source: "input", target: "gemini" },
        { source: "gemini", target: "gmail" },
      ],
      variables: [{ key: "recipient", value: "me@example.com", type: "workflow" }],
    },
    ...overrides,
  };
}
