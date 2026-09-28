# flowforge-workflow-engine

The FlowForge AI workflow engine: a node registry, `{{...}}` variable resolution, graph
validation, and a sequential executor. It has no web or database dependencies; the API
in `apps/api` imports it.

```python
from flowforge_engine import ExecutionServices, NodeContext, ProviderSettings, execute_graph, validate_workflow
from flowforge_engine.testing import example_graph

graph = example_graph()                      # Input → Gemini → Gmail → Output
assert validate_workflow(graph) == []
# Real providers, keys from the environment (GEMINI_API_KEY, SMTP_USER, SMTP_PASSWORD, ...):
services = ExecutionServices(provider_settings=ProviderSettings.from_env())
result = await execute_graph(graph, NodeContext(workflow_id="w1", execution_id="e1", services=services))
result.final_output["result"]["summary"]    # Gemini's text; the email was really sent
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
| `gemini`       | ai          | **user_prompt**, provider, model, system_prompt, temperature, max_tokens, fallback |
| `groq`         | ai          | same as gemini                                                       |
| `openrouter`   | ai          | same as gemini (`:free` models welcome)                              |
| `ollama`       | ai          | same as gemini (local, no key)                                       |
| `openai`       | ai          | same as gemini                                                       |
| `anthropic`    | ai          | same as gemini (temperature is ignored by current Claude models)     |
| `gmail`        | integration | **to**, **subject**, **body**, auth, cc, bcc, html_body, attachments |
| `gmail_read`   | integration | auth, folder, from_address, subject, unread_only, since_days, max_results, mark_as_read, include_body, max_body_chars |
| `http_request` | integration | **url**, method, headers, query, body, timeout_seconds, fail_on_error |

`default_registry.describe()` returns every type with its JSON config schema.

## Providers

`get_llm_provider(name, settings)` returns the real adapter for `gemini`, `groq`,
`openrouter`, `ollama`, `openai`, or `anthropic`, and raises `MissingCredentialsError`
("Authentication missing for provider 'groq': ...") when its key isn't configured. Ollama
needs no key. Groq, OpenRouter, Ollama, and OpenAI share `OpenAICompatibleProvider`
(configurable `base_url`, `api_key`, model). Every adapter implements `generate()`,
`stream()` (async text deltas), `embed()` (Gemini, Ollama, and OpenAI; others raise
`ProviderNotSupportedError`), and `verify()` (a cheap real call used by connection tests).

`get_email_provider("gmail", settings)` / `get_mailbox_provider("gmail", settings)` return
`SMTPEmailProvider` / `IMAPEmailProvider` (Gmail with an App Password by default; any
SMTP/IMAP host works). Blocking I/O runs in `asyncio.to_thread`.

`ProviderSettings` holds keys, base URLs, default models, the mailbox, and the `RetryPolicy`.
`ProviderSettings.from_env()` reads `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`,
`OLLAMA_BASE_URL`, `*_MODEL`, `SMTP_*`, `IMAP_*`, and `LLM_*` retry settings, and
`with_account(name, **fields)` overlays per-user credentials. `validate_graph(...,
services=ExecutionServices(...))` reports providers without credentials as `auth_missing`
issues.

Transient failures (429, 408, 5xx, network) are retried by `with_retries` with exponential
backoff and jitter, honouring `Retry-After` / Gemini `RetryInfo` up to `max_delay`. SDK-level
retries are off, so this is the only retry layer. LLM nodes accept a `fallback` chain
(`["groq", "openrouter:google/gemma-4-31b-it:free", "ollama"]`) and report `provider_used`.

Mocks (`MockLLMProvider`, `MockEmailProvider`) are handed out only when `settings.testing`
is set (`TESTING=true`, the pytest suite) or a node picks provider `mock` / email auth `mock`.
`flowforge_engine.testing.mock_services()` builds testing services explicitly.

## Tests

```bash
pip install -e ".[providers,test]"
pytest                      # add -m "not network" to skip the real HTTP call
pytest -m live -s           # real providers with the keys in the environment / nearest .env
```

Live tests skip, naming the missing variable, when a key is blank; the Ollama test skips when
Ollama isn't reachable.
