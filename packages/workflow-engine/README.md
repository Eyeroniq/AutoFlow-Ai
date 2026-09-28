# flowforge-workflow-engine

The FlowForge AI workflow engine: a node registry, `{{...}}` variable resolution, graph
validation, and a sequential executor. It has no web or database dependencies; the API
in `apps/api` imports it.

```python
from flowforge_engine import NodeContext, execute_graph, validate_workflow
from flowforge_engine.testing import example_graph

graph = example_graph()                      # Input → Gemini → Gmail → Output
assert validate_workflow(graph) == []
result = await execute_graph(graph, NodeContext(workflow_id="w1", execution_id="e1"))
result.final_output["result"]["summary"]    # "[MOCK RESPONSE to: Write a three-sentence ...]"
```

## Graph format

```json
{
  "nodes": [{"id": "gemini", "type": "gemini", "label": "Summarize", "position": {"x": 0, "y": 0},
             "config": {"user_prompt": "Summarize {{input.topic}}"}}],
  "edges": [{"source": "input", "target": "gemini", "source_handle": null}],
  "variables": [{"key": "recipient", "value": "me@example.com", "type": "workflow"}]
}
```

Node ids may contain letters, digits, `_` and `-` (they appear inside references). `vars`
and `system` are reserved. Edges accept React Flow's `sourceHandle`/`targetHandle` too.

## References

| Reference                  | Resolves to                                                    |
| -------------------------- | -------------------------------------------------------------- |
| `{{gemini.response}}`      | the `response` key of upstream node `gemini`'s output          |
| `{{http.body.items[0].id}}`| dot paths into JSON; list items via `[0]` or `.0`              |
| `{{input.topic}}`          | an Input node's value (`{{<id>.value}}` or `{{<id>.<name>}}`) |
| `{{vars.recipient}}`       | a workflow variable                                            |
| `{{system.execution_id}}`  | `workflow_id`, `execution_id`, or `node_id`                    |

A config string that is exactly one reference keeps the referenced value's type (a dict
stays a dict). References embedded in text are stringified (objects as JSON).

## Node types

| Type           | Category    | Config (required in bold)                                            |
| -------------- | ----------- | -------------------------------------------------------------------- |
| `input`        | io          | name, input_type (text/number/json), default, required               |
| `output`       | io          | **value**, name (default `result`)                                   |
| `text`         | io          | **text**                                                             |
| `condition`    | logic       | **left**, **operator** (equals/not_equals/contains/greater_than/less_than), **right**; outgoing edges need `source_handle` `true`/`false` |
| `delay`        | logic       | **seconds** (0–10)                                                   |
| `gemini`       | ai          | **user_prompt**, model, system_prompt, temperature, max_tokens       |
| `openai`       | ai          | same as gemini                                                       |
| `anthropic`    | ai          | same as gemini (temperature is ignored by current Claude models)     |
| `gmail`        | integration | **to**, **subject**, **body**, cc                                    |
| `http_request` | integration | **url**, method, headers, query, body, timeout_seconds, fail_on_error |

`default_registry.describe()` returns every type with its JSON config schema.

## Providers

`get_llm_provider("gemini" | "openai" | "anthropic", settings)` returns the real SDK
adapter when the matching API key is set (`GEMINI_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`), and otherwise falls back to `MockLLMProvider` with a warning. The mock
returns `[MOCK RESPONSE to: <first 50 chars of the prompt>]`. Email always uses
`MockEmailProvider` in this phase; sent messages are kept in `MockEmailProvider.outbox()`.

## Tests

```bash
pip install -e ".[providers,test]"
pytest                      # add -m "not network" to skip the real HTTP call
```
