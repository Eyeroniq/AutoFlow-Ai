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
| `condition`    | logic       | **left**, **operator** (equals, not_equals, contains, not_contains, starts_with, ends_with, greater_than, greater_or_equal, less_than, less_or_equal, is_empty, is_not_empty, matches), right, case_sensitive; outgoing edges need `source_handle` `true`/`false` |
| `delay`        | logic       | **seconds** (0–60)                                                   |
| `gemini`       | ai          | **user_prompt**, provider, model, system_prompt, temperature, max_tokens, fallback |
| `groq`         | ai          | same as gemini                                                       |
| `openrouter`   | ai          | same as gemini (`:free` models welcome)                              |
| `ollama`       | ai          | same as gemini (local, no key)                                       |
| `openai`       | ai          | same as gemini                                                       |
| `anthropic`    | ai          | same as gemini (temperature is ignored by current Claude models)     |
| `gmail`        | integration | **to**, **subject**, **body**, auth, cc, bcc, html_body, attachments |
| `gmail_read`   | integration | auth, folder, from_address, subject, unread_only, since_days, max_results, mark_as_read, include_body, max_body_chars |
| `http_request` | integration | **url**, method, headers, query, body, timeout_seconds, fail_on_error |
| `telegram`     | integration | **text**, chat_id (blank: the account's default chat), format (markdown/html/text), disable_link_preview, silent, split_long, auth |
| `discord_webhook` | integration | content and/or embed_title, embed_description, embed_url, embed_color, embed_footer; webhook_url (blank: the configured one), username, avatar_url, thread_id, allow_mentions, split_long, auth |
| `for_each`     | lists       | **items**, **prompt** (per item), mode (llm/template), system_prompt, output_format (text/json), provider, model, fallback, temperature, max_tokens, concurrency, rate_limit_per_minute, max_items, item_timeout_seconds, timeout_seconds, fail_when, flatten |
| `filter`       | lists       | **items**, **operator**, field, value, case_sensitive, flatten       |
| `join`         | lists       | **items**, template (per item), separator, numbered, header, footer, empty_text, max_items, max_chars, flatten |
| `rss`          | sources     | **url**, max_items, since_last_run, include_content, max_summary_chars, timeout_seconds |
| `web_page`     | sources     | **url**, max_chars, include_links, include_tables, timeout_seconds  |
| `pdf_extract`  | documents   | **file**, pages, max_chars                                           |
| `ocr`          | documents   | **file**, language, pages, dpi, psm, max_pages, prefer_text_layer (PDF pages with a text layer are read directly) |
| `summarize`    | documents   | **text**, length, style, focus, language, provider, model, fallback  |
| `extract_entities` | documents | **text**, entity_types, custom_types, provider, model, fallback    |

`default_registry.describe()` returns every type with its JSON config schema.

**Per-item templates.** A node type's `deferred_fields` (For Each's `prompt` and
`system_prompt`, Join's `template`; `describe()` calls them `item_fields`) are left alone by
the executor, and the node resolves them once per list item with `{{item}}`, `{{item.<field>}}`,
and `{{index}}` in scope next to the usual references. Validation accepts those roots only
in such fields. A node type can also set its own timeout (`NodeDefinition.timeout`): For
Each uses its `timeout_seconds`.

**Node state.** `ExecutionServices.state` is a `NodeStateStore` (`load(node_id)`,
`save(node_id, state)`): what a node keeps between runs of a workflow, such as the RSS
entries it has returned. `MemoryStateStore` is the default; the API stores it per workflow
and node and serves what the last *successful* run saved.

## Observing and stopping a run

`execute_graph(graph, context, hooks=..., control=...)` takes two optional objects:

- `ExecutionHooks` (subclass it): `node_started(node, label, started_at)`,
  `node_finished(result)` (success, failure, or skip), and `node_token(node_id, text, provider)`
  for streamed LLM deltas (only called when overridden, and for LLM nodes with `"stream": true`).
  The API's worker uses these to write node rows and publish live events.
- `ExecutionControl`: `request_stop(reason, status=RunStatus.STOPPED)` from another task skips
  every node that hasn't started and cancels the running one if its type is `interruptible`
  (Gmail sending isn't: it finishes first). The result's status is `stopped`, or whatever
  `status` was passed (e.g. `FAILED` for a time limit).

Node types also declare `queue` (default `"default"`); `queue_for_graph(graph)` picks the
task queue for a run.

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
SMTP/IMAP host works). Blocking I/O runs in `asyncio.to_thread`. `MailboxQuery.uid_after` and
`oldest_first`, with `mailbox_status(folder)` (UIDVALIDITY and UIDNEXT), let the API's email
trigger poll for new mail only.

`get_telegram_provider` returns `TelegramProvider` (Bot API over httpx: `send_message`,
`verify` = `getMe` plus `getChat`), and `get_discord_provider` a `DiscordWebhookProvider`
(`execute` with `?wait=true`, `verify` = a `GET` of the webhook; only `discord.com` webhook
URLs are accepted). Both retry `429`s after the wait the service asks for, and redact their
secret from errors.

`ProviderSettings` holds keys, base URLs, default models, the mailbox, and the `RetryPolicy`.
`ProviderSettings.from_env()` reads `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`,
`OLLAMA_BASE_URL`, `*_MODEL`, `SMTP_*`, `IMAP_*`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`DISCORD_WEBHOOK_URL`, and `LLM_*` retry settings, and
`with_account(name, **fields)` overlays per-user credentials. `validate_graph(...,
services=ExecutionServices(...))` reports providers without credentials as `auth_missing`
issues.

Transient failures (429, 408, 5xx, network) are retried by `with_retries` with exponential
backoff and jitter, honouring `Retry-After` / Gemini `RetryInfo` up to `max_delay`. SDK-level
retries are off, so this is the only retry layer. LLM nodes accept a `fallback` chain
(`["groq", "openrouter:google/gemma-4-31b-it:free", "ollama"]`) and report `provider_used`.
The executor puts the node's deadline on `NodeContext.deadline`, and the chain shares it out:
each provider with fallbacks after it gets an equal share of the time left (the last gets all
of it), so a provider that is slow to fail can't use up the time the fallbacks need.

Mocks (`MockLLMProvider`, `MockEmailProvider`, `MockTelegramProvider`, `MockDiscordProvider`) are handed out only when `settings.testing`
is set (`TESTING=true`, the pytest suite) or a node picks provider `mock` / email auth `mock`.
`flowforge_engine.testing.mock_services()` builds testing services explicitly.

## Tests

```bash
pip install -e ".[providers,documents,sources,test]"
pytest                      # add -m "not network" to skip the real HTTP call
pytest -m live -s           # real providers with the keys in the environment / nearest .env
```

Live tests skip, naming the missing variable, when a key is blank; the Ollama test skips when
Ollama isn't reachable.
