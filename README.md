# FlowForge AI

[![CI](https://github.com/Eyeroniq/AutoFlow-Ai/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Eyeroniq/AutoFlow-Ai/actions/workflows/ci.yml)

A visual AI workflow automation builder. This repository is being built in phases.

- **Phase 1:** monorepo skeleton, Docker Compose stack, the full database schema, JWT
  authentication (with refresh), and a minimal Next.js frontend that proves auth end to end.
- **Phase 2:** the workflow engine package (node registry, `{{...}}` variable resolution,
  graph validation, execution) and workflow CRUD + synchronous execution APIs.
- **Phase 2.5:** real, free-tier-friendly providers instead of mocks: Google
  Gemini, Groq, OpenRouter (`:free` models), and local Ollama for LLMs (plus OpenAI and
  Anthropic if you add keys), and Gmail over SMTP/IMAP with an App Password. Per-user
  credentials are encrypted at rest, and a provider without credentials is a validation
  error, never a silent mock.
- **Phase 3:** asynchronous execution. `POST /run` queues the run on Redis and
  returns `202` at once; Celery workers execute it, recording every node's state in Postgres
  and publishing live events that `WS /ws/executions/{id}` streams to clients (with a
  database replay for late joiners). Runs can be stopped, crashed workers are detected, and
  delivery is idempotent. Everything is testable from `/docs` and `scripts/watch_run.py`.
- **Phase 4:** the visual editor. A full-screen React Flow canvas at
  `/pipelines/{id}` wired to the real API: a node library from `GET /api/nodes`, config forms
  generated from each node's JSON Schema, `{{`-autocomplete for references, autosave with
  undo/redo, live validation with errors on the nodes, one-node test runs, and runs whose
  node and edge colors follow the WebSocket live. Plus a dashboard, execution history and
  detail pages, and an integrations page for connecting provider keys.
- **Phase 3.5:** document AI and independent scaling. File uploads
  (`POST /api/files`), Input nodes of type File, and four document nodes: PDF Extract
  (PyMuPDF), OCR (Tesseract), Summarize, and Entity Extraction (validated JSON). Workers are
  split per queue (`worker-default`, `worker-llm`, `worker-ocr`), and a run hands itself from
  queue to queue, so OCR and LLM capacity scale separately. Plus a Locust load test with
  measured results, and an SSRF guard on the HTTP Request node.
- **Deployments:** **Deploy** in the editor publishes a pipeline as
  `POST /api/v1/deployments/{deployment_id}/run`, authenticated with a per-deployment API key
  that is shown once and stored hashed. Calls run through the same Celery path, either queued
  (`202` + `execution_id`) or with `?wait=true` (the final output in the response), and are
  rate limited per deployment. See [Deploying a pipeline](#deploying-a-pipeline). Plus UI
  polish: no Next.js dev badge, wrapping final output, and node summaries that truncate
  cleanly with the full text on hover.
- **Phase 5:** day-to-day automation on free services.
  [Triggers](#triggers) run pipelines without a click: a cron **schedule** in any time zone
  (Celery beat, fired exactly once per fire time), **new email** in Gmail (one run per
  message, de-duplicated by Message-ID), and the deployment endpoint as a **webhook**, with a
  per-workflow runs-per-hour cap and automatic switch-off after repeated failures.
  [List nodes](#lists-for-each-filter-join): **For Each** (a prompt or an LLM call per item,
  with concurrency and per-minute rate limits, failures recorded per item), **Filter**, and
  **Join / Format**. [Free data sources](#free-data-sources-rss-and-web-pages): **RSS Feed**
  (with "since last run") and **Web Page** (readable text). [Notifications](#notifications-telegram-and-discord):
  **Telegram** and **Discord Webhook** nodes with real connection tests. Four seeded
  [templates](#templates) with one-click **Use template**: Morning Digest, Invoice Extractor
  (with a CSV download), Email Triage, and Job Alert Filter.
- **Phase 6:** audio, web search, and more free LLMs.
  [Speech to Text](#audio-speech-to-text) transcribes audio and video (Groq's free Whisper, or
  faster-whisper on the worker's CPU with no key), with ffmpeg splitting long recordings at
  pauses and stitching the timestamps back together, on its own `audio` queue and
  `worker-audio` service. The run form can **record** from the microphone or a tab, behind a
  consent checkbox. [Web Search](#web-search) queries DuckDuckGo (no key) with automatic fallback
  to Tavily, and can read the top pages. **Structured Output** returns JSON validated against a
  schema. New LLM providers: **Mistral**, **Cerebras**, and **Custom (OpenAI-compatible)** with
  the SSRF guard on its base URL (GitHub Models was retired by GitHub on 2026-07-30, so it
  isn't offered). Two more templates: **Meeting Notes** and **Web Research**.
- **Knowledge bases:** [RAG](#knowledge-bases-rag) on Postgres + pgvector. Upload
  PDFs, scans, or text on the **Knowledge** page: the ocr workers read them (text layer, OCR for
  scanned pages), chunk them at sentence boundaries, and embed them (Gemini, OpenAI, or Ollama,
  768 dimensions, HNSW cosine index), with live status and a test search. Five nodes (Add
  Document, Chunker, Embedding, Retriever, Reranker) and two templates: **PDF to Knowledge
  Base** and **Document Q&A**, whose answers cite `[n]` sources that lead back to the exact chunk.
- **Privacy layer:** [privacy and security](#privacy-and-security). One detection
  service finds secrets (key formats, JWTs, private keys, passwords, high-entropy tokens), card
  numbers (Luhn), Aadhaar (Verhoeff) and PAN, and, when switched on, personal data (Presidio +
  spaCy). A **privacy guard** on every outbound and LLM node blocks, redacts, or warns before the
  node runs; stored step data is masked by default; each run gets a **Privacy Report** (counts and
  types only). New nodes: **Secret Scanner**, **PII Redact** / **PII Restore** (the mapping in
  encrypted Redis for an hour, so it survives queue hand-offs), and **ICS Calendar Event**; the
  Telegram node sends files; `{{system.now}}` and `{{system.today}}`.
- **Telegram Command Center:** [message your bot](#telegram-command-center) to run
  deployed pipelines. A `telegram-listener` service long-polls the bot (each update handled once,
  the offset kept in Redis); a **Telegram message** trigger allowlists chats; an LLM **intent router**
  matches the message against each deployment's (now required) description, fills its inputs, and
  asks one question when unsure. Voice notes are transcribed, photos fill file inputs. Pipelines with
  **side effects** (detected on deploy) wait for a signed, chat-bound **Confirm** button that expires
  after 5 minutes. Per-chat rate limit; the privacy layer checks messages in and replies out.
- **Discord voice meetings:** a `discord-bot` service watches one voice channel and, only after you tap 
  **Yes** in Telegram (signed, chat-bound, 5-minute buttons), joins, posts a visible notice, records, and sends 
  the Meeting Notes summary back to that chat ([setup and flow](#discord-voice-meetings)).
- **Multi-agent resume refinement:** upload a resume PDF (and optionally a job description) and five specialist agents
  (parser, ATS checker, content coach, job matcher, rewriter) each show their findings, then a before/after
  rewrite you can download as PDF/DOCX or email to yourself ([how it works](#multi-agent-resume-refinement)).
- **Generate with AI, Run Replay, command palette (this state):** [describe a pipeline](#generate-with-ai-run-replay-and-the-command-palette)
  and an LLM drafts it from the node catalog, checked against the validator and retried until it
  validates; **Replay** animates a finished run from its stored timings with play/pause/scrub and
  speeds; **Ctrl+K** jumps to any pipeline or page and runs quick actions.

## Stack

| Layer    | Tech                                                                        |
| -------- | --------------------------------------------------------------------------- |
| Frontend | Next.js 16 (App Router), TypeScript, Tailwind CSS v4, React Flow (`@xyflow/react` 12), Zustand, TanStack Query, React Hook Form + Zod, lucide-react |
| Backend  | Python 3.13, FastAPI, SQLAlchemy 2.0 (async, asyncpg), Alembic, Pydantic v2 |
| Engine   | `packages/workflow-engine` (Pydantic v2, httpx); google-genai, openai, anthropic SDKs; stdlib smtplib/imaplib |
| Secrets  | Fernet (`cryptography`) for stored credentials                                |
| Auth     | JWT (python-jose, HS256), passlib + bcrypt, slowapi rate limiting           |
| Async    | Celery 5.6 (Redis broker + result backend), Redis pub/sub, WebSockets; one worker service per queue |
| Documents | PyMuPDF (PDF text, page rendering), Tesseract 5 via pytesseract (OCR), Pillow |
| Data     | PostgreSQL 16 with pgvector (knowledge-base embeddings, HNSW), Redis 7     |
| Tests    | pytest + pytest-asyncio against a real Postgres test database; Vitest (web units); Playwright (end to end, real stack); Locust (load) |
| Infra    | Docker Compose                                                              |

## Prerequisites

- **Docker Desktop** (or Docker Engine) with **Compose v2.24+**. Check with `docker compose version`.
- For running services outside Docker (optional): **Python 3.11+** and **Node.js 20.9+** (the images use Python 3.13 and Node 24).

## Quick start

```bash
# 1. Create your env file
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env

# 2. Set the two required secrets in .env:
#    JWT_SECRET      any long random string
python -c "import secrets; print(secrets.token_urlsafe(64))"
#    ENCRYPTION_KEY  a Fernet key (encrypts stored credentials)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 3. Add at least one LLM key (e.g. GEMINI_API_KEY) and, for email, SMTP_USER +
#    SMTP_PASSWORD (a Gmail App Password). See "Providers and free API keys" below.

# 4. Boot everything (first run builds the images)
docker compose up --build

# 5. In another terminal, create the demo user
docker compose exec api python -m app.db.seed
```

Then open:

| What            | URL                                      |
| --------------- | ---------------------------------------- |
| Web app         | http://localhost:3000                    |
| API             | http://localhost:8000                    |
| API docs        | http://localhost:8000/docs (Swagger UI)  |
| Health check    | http://localhost:8000/api/health         |
| Postgres (host) | `localhost:5433`, user/pass `flowforge` |
| Redis (host)    | `localhost:6379`                         |

Log in with **demo@flowforge.ai** / **demo1234**, or register a new account. The dashboard
lists **Demo: Summarize and email** (Input → Gemini → Gmail → Output). Open it, press
**Validate**, then **Run**: with `GEMINI_API_KEY`, `SMTP_USER`, and `SMTP_PASSWORD` set, the
nodes turn blue then green as the run progresses and the summary arrives in `SMTP_USER`'s inbox.
**Demo: Scanned invoice to entities** (Input(File) → OCR → Summarize → Entity Extraction →
Output) runs on the bundled sample scan: OCR on `worker-ocr`, the two LLM nodes on
`worker-llm`. See [Document AI](#document-ai).

### About `docker compose up`

The stack is defined in [`infrastructure/docker-compose.yml`](infrastructure/docker-compose.yml).
The root [`compose.yaml`](compose.yaml) just `include`s it and passes it the root `.env`, so
`docker compose` commands work from the repo root. Without the root file, Compose would look
for `.env` in `infrastructure/`. The equivalent long form is:

```bash
docker compose -f infrastructure/docker-compose.yml --env-file .env up --build
```

What happens on `up`:

1. `postgres` and `redis` start and wait until healthy.
2. `api` runs `alembic upgrade head`, then starts uvicorn with `--reload`.
3. The workers, `worker-default`, `worker-llm`, and `worker-ocr` (Celery, one per queue; see
   [Workers: queues and scaling](#workers-queues-and-scaling)), `beat` (Celery beat: the
   one-minute [triggers](#triggers) tick), and `web` (`next dev`) start once the API
   healthcheck passes.

`api`, the workers, and `web` bind-mount their source directories, so edits hot-reload (the
workers are restarted by `watchfiles`). The API and workers also mount `packages/workflow-engine`
(installed editable), so engine edits reload them too, and they share the `files_data` volume
(uploads) at `/data/files`.

Useful commands:

```bash
docker compose up -d                   # run in the background
docker compose logs -f api             # follow API logs (structured JSON)
docker compose logs -f worker-ocr      # follow one worker service (worker-default / -llm / -ocr)
docker compose ps                      # service status and health
docker compose down                    # stop (keeps the database volume)
docker compose down -v                 # stop and delete all data
docker compose up --build -V web       # after changing package.json (renews node_modules volume)
docker compose up --build api worker-default worker-llm worker-ocr   # after changing requirements.txt, the engine's dependencies, or the Dockerfile
```

## The editor

![The editor with the seeded pipeline](docs/screenshots/editor.png)

| Running live (blue = running, green = done) | Validation errors on the nodes |
| --- | --- |
| ![A run in progress](docs/screenshots/run-live.png) | ![A cycle and a missing field](docs/screenshots/validation-errors.png) |

![Execution detail](docs/screenshots/execution-detail.png)

Everything in the UI comes from the API; there is no mock data.

- **Canvas** (`/pipelines/{id}`): drag nodes from the library (grouped General / LLM /
  Integrations, searchable; click to add at the center), move, connect and disconnect edges,
  select several (Shift-drag a box, or Ctrl/Cmd-click), duplicate (Ctrl/Cmd+D), rename
  (double-click the title), collapse, and delete (Delete/Backspace or the node's ⋯ menu).
  Undo/redo (Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z or Ctrl+Y) covers adds, deletes, moves, renames,
  config and variable edits, with a bounded history (100 steps; typing in one field within a
  second is one step, and so is a drag). The view fits the graph on load and when a run
  starts; zoom controls and a minimap sit bottom-right.
- **Node cards** show the category color, icon, title, a one-line config summary, input and
  output handles (true/false handles on Condition), a status dot, an issue badge, and streamed
  LLM text while a node runs. A summary that doesn't fit ends in an ellipsis, and hovering it
  shows the whole text in a tooltip (only when something was cut off). Edges are gray before
  a run, blue and animated while their target runs, green when both ends succeeded, and red
  when either failed.
- **Config panel** (select a node): a form generated from the node's JSON Schema with React
  Hook Form + Zod (text, textarea, number, select for enums, checkbox, lists, JSON), required
  markers, and inline errors. LLM nodes get a provider select that shows which credential it
  would use, a model field, and an ordered fallback chain. LLM and Gmail nodes have a real
  **Test connection** (`POST /api/integrations/{provider}/test`). **Test node** runs just that
  node on the server with sample upstream outputs and shows the resolved input, output,
  duration, and error.
- **References:** type `{{` in any text field for a keyboard-navigable list of what the node
  can use: `{{input.<name>}}`, upstream outputs such as `{{gemini.response}}`,
  `{{vars.<key>}}`, and `{{system.execution_id}}`. The **Variables** panel edits workflow
  variables, which are saved with the graph.
- **Saving:** edits autosave one second after you stop (or press Save / Ctrl/Cmd+S). The top
  bar shows Unsaved changes / Saving… / Saved. A failed save shows **Save failed** with a
  Retry, plus a toast, and retries with backoff; it is never silent. Only one save is in
  flight at a time, and responses for a previously loaded workflow are ignored.
- **Validation:** the unsaved graph is checked by `POST /validate` as you edit. Issues badge
  their nodes and appear under the fields they concern, in the backend's words. The Validate
  button opens the list; clicking an issue selects and centers its node.
- **Running:** Run asks for the Input nodes' values, saves, and calls `POST /run` (`202`). The
  editor then follows the run over `WS /ws/executions/{id}` (the JWT is sent as the first
  message): node and edge colors, a run panel with a per-node timeline (status, duration,
  input, output, error, streamed tokens), and the final output (long values wrap rather than
  scroll sideways). Stop calls `/stop`. Dropped connections reconnect with backoff, and the
  server's snapshot replays anything missed; a run already in progress when you open the
  editor is joined the same way. A `4401` refreshes the session once and then asks you to
  sign in; a `4404` says the run isn't yours.
- **Deploying:** **Deploy** in the top bar publishes the pipeline as an API endpoint; see
  [Deploying a pipeline](#deploying-a-pipeline).
- **Other pages:** `/dashboard` (pipelines with status, last run, and last modified; create,
  open, run, duplicate, delete; recent executions), `/executions` (history filtered by
  pipeline and status, refreshing while runs are active), `/executions/{id}` (per-node
  timeline, live while running, with Stop), and `/integrations` (connect, test, and disconnect
  Gemini, Groq, OpenRouter, Ollama, OpenAI, Claude, and Gmail; secrets are write-only and
  shown masked). Errors surface as toasts, and the editor has its own error boundary.

## Document AI

Four node types read uploaded documents. They are in the editor's **Documents** group, with
config forms generated from their schemas like every other node:

| Node | Type | Queue | What it does |
| ---- | ---- | ----- | ------------ |
| PDF Extract | `pdf_extract` | `ocr` | The PDF's text layer (PyMuPDF), for a page range such as `"1-3,5"` or `"2-"`. `needs_ocr` flags scans (almost no text per page). Output: `text`, `pages[]` (text per page), `page_count`, `char_count`, `truncated`. |
| OCR | `ocr` | `ocr` | Tesseract 5 on images (PNG, JPEG, TIFF including multi-page, WebP, BMP, GIF) and scanned PDFs (each page rendered at `dpi`, default 300), in `language`: `eng`, `deu`, `fra`, `spa`, `ita`, `por`, or combinations like `eng+deu` (missing language data is a clear error that lists what's installed). Output: `text` with lines and paragraphs kept, `pages[]` with each page's `confidence`, `mean_confidence`, `engine`. |
| Summarize | `summarize` | `llm` | An LLM summary: `length` (short, medium, long), `style` (paragraph, bullets, executive), optional `focus` and `language`. It uses the provider abstraction with the same `provider` / `model` / `fallback` chain as the LLM nodes. Long input is cut to `max_input_chars` and reported as `truncated`. |
| Entity Extraction | `extract_entities` | `llm` | Structured JSON: `people`, `organizations`, `dates` (with an ISO date), `amounts` (numeric `value` and ISO `currency`), and custom types (`"invoice_number: the invoice or reference number"`). The reply is parsed and validated against a Pydantic schema. If it isn't valid JSON or doesn't match, the model is asked once more with the problem spelled out; a second bad reply fails the node with that problem. Output: `entities`, `counts`, `attempts`. |

PDF Extract and OCR do their CPU work in a thread, so the worker keeps heartbeating and
checking for stops, and a stopped run stops at the next page.

**Files.** `POST /api/files` takes the multipart field `file`:

- **Type** comes from the file's first bytes, never from the client's `Content-Type` or the
  extension: PDF, PNG, JPEG, TIFF, WebP, BMP, GIF, or UTF-8 text. Anything else is `415`.
- **Size:** up to `MAX_UPLOAD_MB` (25). A request declaring more is refused before the body
  is read, and the body is counted while it streams to disk (`413`). A request without
  `Content-Length` is `411`, and an empty file `400`.
- **Storage:** `FILES_DIR/<owner id>/<file id>` on the `files_data` volume, which the API and
  every worker mount at `/data/files` (the OCR worker reads what the API stored). The path
  never includes the uploaded name, so `../../x.pdf` can't escape. The name is kept only
  for display, sanitized (no directories or control characters).
- **Access:** every read goes through the owner. Another user's file is a `404` from
  `GET /api/files/{id}`, `/content`, and `DELETE`. It's also refused as a run input (`422`
  before anything is queued), and a node can't open it at run time either.
- **Input nodes of type File** take an upload's id, from the run's `inputs` or the node's
  default, and output `{file_id, filename, content_type, size_bytes}`. So `file:
  "{{input.document}}"` wires a document node to it. In the editor, the run form offers
  your uploads or a new upload with a progress bar, and so does the Input node's default.
  Extracted text, summaries, and entities show up readably in the run panel and on the
  execution page (raw JSON is a click away).

**Sample and demo.** [`samples/scanned-invoice.pdf`](samples/scanned-invoice.pdf) is a
two-page invoice and cover letter, made by
[`samples/make_scanned_sample.py`](samples/make_scanned_sample.py): typeset, then degraded
the way a flatbed scan is (slight skew, uneven exposure, sensor noise, softness, JPEG
compression) and saved as images only. It has no text layer, so PDF Extract returns nothing
(`needs_ocr: true`) and only OCR can read it. It is synthetic (no paper went through a
scanner), so upload your own scans to try real ones. The seed stores it as one of the demo
user's uploads and creates **Demo: Scanned invoice to entities**: Input(File) → OCR →
Summarize → Entity Extraction → Output, with Gemini and, when the server has `GROQ_API_KEY`,
Groq as the fallback.

```bash
python scripts/run_document_demo.py                      # the sample, through the real workers
python scripts/run_document_demo.py --file my-scan.pdf   # upload and use your own file
```

A run of it, as printed by that script (Gemini was answering `503 high demand` at the time,
so both LLM nodes fell back to Groq's `openai/gpt-oss-20b`):

```
queued 2f409b19-6967-46a9-b034-9443af84e819 on 'ocr'
success in 88.0s (1 hand-off(s))

node       status   queue    worker                        duration
input      success  ocr      worker-ocr@e2dfac4b81ac       0.02s
ocr        success  ocr      worker-ocr@e2dfac4b81ac       1.54s
summarize  success  llm      worker-llm@cfb509005a1d       12.31s
entities   success  llm      worker-llm@cfb509005a1d       73.11s
output     success  llm      worker-llm@cfb509005a1d       0.00s

pages: 2, OCR confidence: 95.4
```

OCR read both pages with a mean word confidence of 95.4. The entities had all three people
(Maria Schneider, Thomas Becker, James O'Connor), the three organizations (one of them the
ship, MV Severn Star, which the model filed as an organization), the invoice and purchase
order numbers (`HF-2026-0417`, `PO-88213`), all six dates with correct ISO values, and the
seven amounts with correct numeric values. The model mislabeled some amounts' `meaning`
(for example, it called 3,200.00 the subtotal); that's the model's reading, which the schema
can't check. Most of Summarize's and Entity Extraction's time was Gemini's retries with
backoff before the fallback.

## Deploying a pipeline

![The Deploy dialog](docs/screenshots/deploy.png)

**Deploy** in the editor's top bar opens a dialog with the pipeline's name, its Input nodes
(name, type, required or optional), its Output nodes, and its endpoint. Deploying saves any
unsaved edits, validates the graph with the same checks as Run (including `auth_missing`), and
publishes it at:

```
POST /api/v1/deployments/{deployment_id}/run
```

- **A snapshot:** the endpoint runs the graph as it was when you deployed it. Editing the
  pipeline doesn't change what callers get until you press **Redeploy**, and the dialog tells
  you when the pipeline has changed since. A redeploy keeps the deployment id (so the URL
  stays the same) and the API key.
- **The API key** (`ffk_` plus 256 random bits) is shown once, in the dialog right after the
  first deploy, with a copy button. The server stores only its SHA-256 hash and an
  8-character prefix for recognizing it (`ffk_Ab12Cd34…`), and the response that carries it
  is `Cache-Control: no-store`. A fast hash is enough because the key is random, not a
  password someone chose. If the key is lost or leaked, **Generate new key** issues a new
  one and revokes the old one at once. It doesn't redeploy, so unfinished edits stay
  unpublished.
- **Authentication:** send the key as `Authorization: Bearer <key>` or `X-API-Key: <key>`.
  A missing or wrong key, another deployment's key, a user's JWT, and an unknown deployment
  id all get the same `401`, so deployment ids can't be probed.
- **Runs** take the same path as the editor's Run: a pending execution (trigger `webhook`,
  with its `deployment_id`; runs from before Phase 5 were renamed from `api`) is queued to the Celery queue its first node needs and recorded by the
  workers. The owner sees it in the executions list and can watch it live. It uses the
  owner's credentials, which the caller never sees, and the response carries only the
  status, final output, and error, not the per-node details.
- **Async (the default):** `202` with the `execution_id` and `links.status`. Poll that with
  the same key.
- **`?wait=true`:** the request waits on the execution's event channel (re-checking the
  database every second) for up to `timeout` seconds (default
  `DEPLOYMENT_WAIT_TIMEOUT_SECONDS`, 30; at most `DEPLOYMENT_MAX_WAIT_SECONDS`, 120) and
  answers `200` with the final output. A run that fails also answers `200`, with
  `status: "failed"` and the `error`. A run still going at the timeout answers `202`, as in
  async mode.
- **Rate limits** apply per deployment (every caller of one endpoint shares them) and count
  only requests with a valid key: `DEPLOYMENT_RUN_RATE_LIMIT` (default `30/minute`) for runs
  and `DEPLOYMENT_STATUS_RATE_LIMIT` (`240/minute`) for status polls. Over the limit: `429`.
  Like the auth limits, they're kept in memory per API process.
- **Other errors:** `422` for a body that isn't `{"inputs": {...}}`, a file input that isn't
  one of the owner's uploads, or a deployed graph that no longer validates (a removed
  credential, say: fix it and redeploy). `503` if the broker is down (the execution is
  marked failed).

Call it with the key from the dialog. The dialog's **Try it** box has this command filled in
with your URL, key, and inputs:

```bash
export FLOWFORGE_API_KEY=ffk_...   # the key the dialog showed

curl -X POST 'http://localhost:8000/api/v1/deployments/<deployment_id>/run?wait=true' \
  -H "Authorization: Bearer $FLOWFORGE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"inputs": {"topic": "the history of workflow automation"}}'
```

```json
{
  "execution_id": "7e6f0fa2-19c7-4393-ac35-6c6638c96c78",
  "deployment_id": "4a635d66-5149-4ef1-a94f-cb423956ad8c",
  "status": "success",
  "final_output": {"result": {"summary": "Workflow automation began with ...", "email": "<...@mail.gmail.com>"}},
  "error": null,
  "created_at": "2026-09-28T16:02:11.402Z",
  "started_at": "2026-09-28T16:02:11.480Z",
  "finished_at": "2026-09-28T16:02:15.912Z",
  "duration_ms": 4432,
  "links": {"status": "/api/v1/deployments/4a635d66-5149-4ef1-a94f-cb423956ad8c/executions/7e6f0fa2-19c7-4393-ac35-6c6638c96c78"}
}
```

Without `?wait=true`, the call returns at once and you poll:

```bash
curl -X POST 'http://localhost:8000/api/v1/deployments/<deployment_id>/run' \
  -H "Authorization: Bearer $FLOWFORGE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"inputs": {"topic": "tides"}}'
# 202 {"execution_id": "...", "status": "pending", "links": {"status": "/api/v1/deployments/.../executions/..."}, ...}

curl 'http://localhost:8000/api/v1/deployments/<deployment_id>/executions/<execution_id>' \
  -H "Authorization: Bearer $FLOWFORGE_API_KEY"
```

In Windows PowerShell, `curl` is an alias for `Invoke-WebRequest`, so use Git Bash or WSL for
the commands above, or:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/api/v1/deployments/<deployment_id>/run?wait=true' `
  -Headers @{ Authorization = "Bearer $env:FLOWFORGE_API_KEY" } -ContentType 'application/json' `
  -Body '{"inputs": {"topic": "the history of workflow automation"}}'
```

There's no undeploy yet: deleting the pipeline deletes its deployment, and rotating the key
locks every caller out. The endpoint is also the pipeline's **webhook trigger**: switching the
webhook off in the Triggers panel (or the failure limit doing so) makes it answer `409`, and
calls count toward the pipeline's triggered runs per hour (`429` beyond). See [Triggers](#triggers).

## Triggers

![The Triggers panel](docs/screenshots/triggers.png)

**Triggers** in the editor's top bar opens a panel with the pipeline's three triggers. Each
has an on/off switch and shows its next run, its last run (status and a link), its
consecutive failures, and anything that went wrong without producing a run. The button's dot
is green when a trigger is on, amber when one is failing, and red when one was switched off
by the failure limit. Triggers run the **saved** pipeline (autosave keeps it current), and
every run records what started it: `trigger` is `manual`, `schedule`, `email`, or `webhook`,
and `trigger_id` names the trigger. The executions list shows it as a badge and filters by it
(`/executions?trigger=schedule`), and so does `GET /api/executions?trigger=...`. Triggered
runs have no `triggered_by_user_id`; they use the owner's credentials.

### Schedule

A 5-field cron expression (`30 7 * * *` is 07:30 daily; `@daily`, `@hourly`, `@weekly`,
`@monthly`, `@yearly` work too) read in an IANA time zone (`Asia/Kolkata`, `Europe/Berlin`,
`UTC`; the panel suggests your browser's), plus optional run `inputs`. The panel previews the
next fire times as you type (`POST /api/triggers/schedule-preview`).

How it fires, and why it never fires twice:

1. The `beat` service (Celery beat, exactly one) sends `flowforge.triggers_tick` on every
   minute (`crontab()`, with a 55 s expiry so a tick stuck in the queue is dropped rather than
   run late). `worker-default` runs it.
2. The tick selects enabled schedules whose `next_fire_at` has passed. Each one is **claimed**
   with a compare-and-set on that exact fire time:
   `UPDATE workflow_triggers SET next_fire_at = <the next one> WHERE id = ? AND next_fire_at = <the time it read>`.
   Postgres re-checks the `WHERE` after a concurrent update commits, so of any number of ticks
   (two beats, a redelivered task, overlapping ticks) exactly one gets the row.
3. The winner records the fire time in `trigger_events` under a unique
   `(trigger_id, event_key)` (`schedule:2026-09-29T02:00:00+00:00`), a second guard, and
   starts the run through the ordinary path: `create_execution` (validation included) and the
   task queue, in the same transaction as the claim.
4. The next fire time is computed from `max(now, fire time)`, so after downtime a schedule
   fires once, not once per missed slot. A fire time older than
   `TRIGGER_MISFIRE_GRACE_SECONDS` (1 h) when a tick sees it is skipped (the panel says so)
   rather than run hours late.

**Daylight saving time** follows classic cron. A fixed time (the hour field names hours, as
in `30 1 * * *`) fires **once**: when clocks fall back and 01:30 happens twice, it fires on the
first one. (Plain croniter fires on both, so FlowForge walks wall-clock times itself.) A time
skipped when clocks spring forward (`30 2 * * *` in New York on DST day) runs at 03:30, the same
offset after the jump. Intervals (the hour field is `*` or `*/n`, as in `*/15 * * * *`) follow
real time, so they keep their spacing through the change.

### New email

Polls the Gmail account (the one under Integrations, or `SMTP_USER`) over IMAP every
`poll_minutes` (at least `EMAIL_TRIGGER_MIN_POLL_MINUTES`) for **unread** mail in `folder`
whose From and/or Subject contain the filters, and starts **one run per new email**, with the
email as the run input named `input_name` (default `email`): an Input node of that name and
type JSON receives `{from, from_address, to, cc, subject, date, body_text, snippet,
message_id, attachments, folder}` (a text Input gets it as readable text). Use it as
`{{email.value.subject}}`, `{{email.value.body_text}}`.

- **From now on:** enabling the trigger reads the folder's `UIDVALIDITY` and `UIDNEXT` right
  away (a mailbox it can't read is a `422` in the panel), and only messages with a higher UID
  count. Mail already in the folder never floods the pipeline.
- **Exactly once:** each poll asks the server for matching messages above the last UID it
  looked at, oldest first, up to `max_per_poll` (the rest wait for the next poll), and records
  each message's **Message-ID** as a unique trigger event before starting its run. However
  often a message is seen again (a redelivered poll task, **Check now** while a poll is
  running, the message moved back into the folder under a new UID, a lost position), it
  never runs twice. A Redis lock keeps two polls of one trigger from overlapping, and a new
  `UIDVALIDITY` (the server renumbered the folder) restarts from its end.
- **Scheduling:** the tick claims due polls the same way as schedules (compare-and-set on
  `next_fire_at`, aligned to the minute) and queues one `flowforge.poll_email_trigger` task
  each, since IMAP can take a few seconds. **Check now** in the panel
  (`POST /api/workflows/{id}/triggers/email/check`) runs a poll immediately.
- **Gmail marks mail you send to yourself as read.** To test the trigger by emailing
  yourself, turn off **Unread only** (keep a subject filter); mail from other people arrives
  unread.

### Webhook

The pipeline's deployment endpoint (`POST /api/v1/deployments/{id}/run` with its API key; see
[Deploying a pipeline](#deploying-a-pipeline)). The panel shows the URL, the key's prefix, and
a curl example, or a **Deploy** button if the pipeline isn't deployed yet. It's on once
deployed; switch it off to make the endpoint answer `409` without deleting the deployment.

### Safety limits (per workflow)

- **Triggered runs per hour** (`max_runs_per_hour`, default 30): schedule, email, and webhook
  runs started in the last rolling hour. Beyond it a schedule or email run is skipped (the
  trigger shows "Skipped: this workflow already started N triggered runs in the last hour")
  and a webhook call gets `429`. The count is taken with the workflow row locked, so
  concurrent triggers can't both slip past it. Manual runs don't count.
- **Switch off after N failures** (`max_consecutive_failures`, default 3; 0 = never): every
  way a run ends goes through `finish_execution`, which, in the same transaction, resets the
  trigger's `consecutive_failures` on success and adds one on failure. At the limit the
  trigger turns itself off and shows **"Disabled after 3 consecutive failed runs. Last error:
  ... Fix the workflow, then turn the trigger back on."** in red, on the dashboard too. A run
  stopped by you doesn't count. A triggered run that can't even start (the pipeline no longer
  validates, say a removed credential) is recorded as a failed execution with its trigger, so
  it's visible in the history and counts too. Turning the trigger back on clears the state.

### Trigger API

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/workflows/{id}/triggers` | All three triggers (`configured`, `enabled`, `config`, `next_run_at`, `upcoming`, `last_run`, `consecutive_failures`, `auto_disabled_at`, `disabled_reason`, `last_error`, `warnings`, `webhook`, `mailbox`), the limits, and `runs_last_hour` |
| PUT | `/api/workflows/{id}/triggers/{schedule\|email\|webhook}` | `{enabled, config?}`: save and switch on or off. `422` for a bad cron, an unknown time zone, or a mailbox that can't be read |
| PUT | `/api/workflows/{id}/trigger-settings` | `{max_runs_per_hour, max_consecutive_failures}` |
| POST | `/api/workflows/{id}/triggers/email/check` | Poll the mailbox now: `{found, runs: [{outcome, execution_id, event_key}]}` |
| POST | `/api/triggers/schedule-preview` | `{cron, timezone, count}` → `{valid, error, cron, interval, next: [...]}` |

## Lists: For Each, Filter, Join

Three nodes in the library's **Lists** group work on lists such as `{{gmail_read.emails}}` or
`{{rss.items}}`. Their per-item fields (For Each's `prompt` and `system_prompt`, Join's
`template`) are resolved once per item with **`{{item}}`** (the item; objects become JSON),
**`{{item.title}}`** (one of its fields), and **`{{index}}`** (0-based) in scope, next to the
usual references (`{{vars.resume}}`). The `{{` autocomplete offers them there, and
validation accepts them only there. `flatten: true` merges nested lists one level, to combine
sources: `items: ["{{gmail_read.emails}}", "{{rss.items}}"]`.

- **For Each** (`for_each`, on the `llm` queue): `mode: llm` sends the prompt for each item to
  an LLM (the usual provider / model / fallback chain) and collects the replies;
  `mode: template` only renders the prompt per item. `output_format: json` parses each reply
  as JSON, so later nodes can read `{{item.output.score}}`. Built for free tiers:
  **`concurrency`** (default 2) items in flight at once, and **`rate_limit_per_minute`**
  (default 10; 0 = none): at most that many calls start in any 60-second window (a sliding
  window; the first calls go out at once, the rest wait). **Partial failures** are recorded per
  item (`results[i]` has `ok`, `output`, and `error`) instead of failing the node; `fail_when`
  (`all_failed` by default, `any_failed`, `never`) decides when the node itself fails.
  `max_items` caps the list (the rest are `skipped`), each item gets `item_timeout_seconds`,
  and the node gets `timeout_seconds` (600): items that can't start in time are marked not run,
  and the others keep their results. Output: `results`, `outputs` (the successful outputs, in
  order), `count`, `succeeded`, `failed`, `skipped`, `rate_limited_seconds`.
- **Filter** (`filter`): keeps the items whose `field` (a path inside each item, such as
  `output.score` or `subject`; blank = the item) matches `operator` and `value`: `equals`,
  `not_equals`, `contains`, `not_contains`, `starts_with`, `ends_with`, `greater_than`,
  `greater_or_equal`, `less_than`, `less_or_equal`, `is_empty`, `is_not_empty`, `matches`
  (regex). Numeric text compares as numbers (`"70"` ≥ `70`), text compares case-insensitively
  unless `case_sensitive`, and items missing the field are dropped. Output: `items`, `count`,
  `removed`, and `errors` for items it couldn't compare. (Condition gained the same operators
  and `case_sensitive`.)
- **Join / Format** (`join`): one text block from a list: each item through `template` (blank
  = the item itself), joined by `separator` (`\n` is a line break), with optional `numbered`,
  `header`, `footer`, and `empty_text` for an empty list. Output: `text`, `count`, `truncated`.

## Free data sources: RSS and web pages

Both download through the same SSRF guard as the HTTP Request node (the URL and every
redirect must reach a public address; see [Security](#security-outbound-requests-ssrf-guard)),
with a browser-like User-Agent and a 5 MB cap.

- **RSS Feed** (`rss`, feedparser): RSS 2.0, RSS 1.0, and Atom. The newest `max_items`
  entries (sorted by date) as `{id, title, link, summary, published, author, tags}` (plus
  `content` with `include_content`), and the feed's title and link. **`since_last_run`** returns
  only entries this node hasn't returned in an earlier **successful** run: the node saves the
  entry ids it saw (`node_states`, per workflow and node), and a run reads what the last
  successful run saved, so if a later node fails the next run sees the same entries again.
  A single-node **Test** reads the position without moving it.
- **Web Page** (`web_page`, trafilatura): fetches a page and returns its main text (navigation,
  footers, and ads removed) with `title`, `author`, `date`, `description`, and `site_name`,
  cut to `max_chars`. Plain text is returned as is; PDFs and other binaries are refused (upload
  them and use PDF Extract).

## Audio: Speech to Text

**Speech to Text** (`speech_to_text`, category Audio, queue `audio`) turns a recording into
text with timestamps.

![A Meeting Notes run: the transcript with timestamps](docs/screenshots/meeting-notes-run.png)

| Config | Default | Meaning |
| ------ | ------- | ------- |
| `file` | required | An uploaded audio or video file: `{{input.recording}}`, or a file id |
| `provider` | `groq` | `groq` (Whisper on Groq's free tier, needs `GROQ_API_KEY`) or `local` (faster-whisper on the worker's CPU, no key) |
| `model` | blank | Blank = `whisper-large-v3-turbo` on Groq (`whisper-large-v3` when translating), `FASTER_WHISPER_MODEL` (`base`) locally: `tiny`, `base`, `small`, `medium`, `large-v3`, `turbo` |
| `task` | `transcribe` | `translate` gives English text from any language (Groq: `whisper-large-v3`; turbo can't translate, and validation says so) |
| `language` | blank | ISO code (`en`, `de`, ...) to skip detection; blank detects it |
| `prompt` | blank | Spellings for names and jargon ("FlowForge, Groq"): Whisper copies their style |
| `chunk_minutes` | `10` | The longest piece sent in one request (Groq only; shortened further so each piece stays under `GROQ_WHISPER_MAX_FILE_MB`) |
| `max_duration_minutes` | `240` | Longer recordings are refused before any work |
| `timeout_seconds` | `900` | The node's own time limit (the run's is `EXECUTION_TIME_LIMIT_SECONDS`, 30 minutes) |

Output: `text`, `segments` (`[{id, start, end, text}]`, seconds from the start of the file),
`language` (ISO code, by the most speech across chunks) and `language_name`,
`duration_seconds`, `chunks`, `provider`, `model`, `task`, `source` (`audio` or `video`), and
`filename`. There are **no speaker labels**: Whisper doesn't identify speakers, and the node
doesn't pretend to. Prompts downstream (like the Meeting Notes template's) say so.

**How a file is processed** (`flowforge_engine.media`, ffmpeg and ffprobe in the image):

1. `ffprobe` reads the duration and streams. A video's audio track is used; a video without
   one fails with "has no audio track".
2. The audio is converted to 16 kHz mono (what Whisper uses internally).
3. For Groq, if the file is longer than one chunk, `silencedetect` finds the pauses, and each
   cut goes in the middle of the latest pause between 50% and 100% of the chunk length
   (a hard cut only where there's no pause at all). Each piece is encoded as 32 kbps MP3
   (~4 KB/s, so 10 minutes is ~2.4 MB, far below the 25 MB free-tier limit).
4. Each piece is transcribed, and the segments are shifted by the piece's start time and
   renumbered, so timestamps run continuously through the whole file.
5. Low-confidence segments at the end of the audio, where Whisper tends to invent a
   "Thank you" in the silence, are dropped (Whisper's own thresholds: `avg_logprob < -1` and
   either `no_speech_prob > 0.6` or reaching the end of the audio), and segment ends are clamped
   to the audio's length.

Local faster-whisper takes the whole file in one pass (no upload limit) with its voice
activity filter on. Its model is downloaded from Hugging Face on first use into
`WHISPER_MODELS_DIR` (Compose: the `whisper_models` volume, shared by the API and the workers),
so the first run of a model size waits for the download (`base` is ~140 MB).

**Uploads.** `POST /api/files` accepts MP3, WAV, M4A/AAC, Ogg (Opus/Vorbis), FLAC, WebM, MP4,
and MOV, detected from the bytes (the file name and the browser's type are ignored). Audio and
video get their own limit, `MAX_MEDIA_UPLOAD_MB` (500), separate from `MAX_UPLOAD_MB` (25) for
documents and images.

**The audio queue.** Speech to Text runs on `worker-audio` (queue `audio`,
`WORKER_AUDIO_CONCURRENCY` = 2 processes, `AUDIO_THREADS_PER_TASK` = 2 CPU threads each for local
transcription), so a long transcription never holds up LLM calls or OCR. A run moves to the
audio worker for this node and on to the next queue after it, like OCR.

### Recording in the browser (and consent)

For a file input, the **Run** form has **Upload** and **Record**. Record opens a small
recorder:

- **"Everyone being recorded has been informed"** must be ticked before **Start recording**
  is enabled. Nothing is captured, and the browser doesn't even ask for the microphone,
  until then.
- **Microphone** uses `getUserMedia`. **Tab or screen audio** uses `getDisplayMedia` where the
  browser supports it (Chrome and Edge on desktop): pick a tab and tick "Share tab audio". The
  video track is dropped at once; only audio is recorded.
- The recording (`MediaRecorder`, WebM/Opus where supported, else Ogg or MP4) is uploaded when
  you press **Stop and upload**, with the same progress bar and cancel button as an upload,
  and selected as the input.
- Clear messages instead of failures: permission denied ("Microphone access was blocked.
  Allow it from the icon in the address bar"), no microphone, the device in use by another
  app, a shared tab without audio, an insecure page (recording needs https or localhost), and
  browsers without `MediaRecorder` or `getDisplayMedia`.

## Web Search

**Web Search** (`web_search`, category Sources) returns `results`: `[{position, title, url,
snippet, source}]`.

| Config | Default | Meaning |
| ------ | ------- | ------- |
| `query` | required | Supports `{{variables}}`, e.g. `{{question.value}}` |
| `provider` | `duckduckgo` | `duckduckgo` (the `ddgs` library, no key) or `tavily` (`TAVILY_API_KEY`) |
| `fallback` | `true` | When the provider fails or rate limits, try the other one (if it has credentials) |
| `max_results` | `5` | 1-20 |
| `fetch_pages` | `0` | Also read the first N result pages (0-10) through the Web Page reader (trafilatura, SSRF-guarded): those results get `page_title` and `page_text`, or `page_error` |
| `max_page_chars` | `4000` | Per page |
| `region`, `safe_search`, `time_range` | `wt-wt`, `moderate`, `any` | Passed to the provider (`time_range`: day, week, month, year) |
| `fail_when_empty` | `true` | Fail when nothing is found, so later nodes don't read an empty list |
| `timeout_seconds` | `20` | Per provider |

Output also has `provider_used` and `fallback_errors`. Errors say what happened and what to do:
DuckDuckGo rate limits (`HTTP 429`) say "wait a minute, or set TAVILY_API_KEY so searches fall
back to Tavily"; Tavily's `401` (bad key), `429`, `432` (plan credits used up), and `433`
(pay-as-you-go limit) are named; timeouts and 5xx are retried. A page that can't be read
(`fetch_pages`) is reported per page instead of failing the search. **Integrations → Web
search → Tavily** stores a key; **Test connection** calls Tavily's `/usage` (it proves the key
and shows the credits used, without spending one).

## Vision, Notion, and Airtable

### Vision

**Vision** (`vision`, LLM group, queue `llm`) asks Gemini about an uploaded image, with
`GEMINI_API_KEY` (or your Gemini credential).

| Config | Default | Meaning |
| ------ | ------- | ------- |
| `image` | required | An uploaded image: `{{input.photo}}` (an Input node of type file) or a file id. PNG, JPEG, and WebP go as they are; TIFF, BMP, and GIF are converted to PNG (a GIF's first frame). Up to 18 MB |
| `prompt` | required | What to do, e.g. "List every item and price on this receipt" |
| `schema` | blank | Optional JSON Schema. The reply must be JSON matching it, with the same instruction, validation, and one retry as the Structured Output node; the parsed value is in `data` |
| `model`, `system_prompt`, `temperature`, `max_tokens` | blank (`GEMINI_MODEL`), blank, `0.2`, `2048` | As on the LLM nodes |

Output: `text` (the reply), `data` (with a schema), `attempts`, `filename`, `content_type`,
`converted`, `model`, `mock`. Seen working: a generated label reading "ORDER 4217" with a
one-field schema returned `{"order_number": "4217"}` on the first attempt
(`gemini-3.5-flash-lite`).

### Notion

Connect a token under **Integrations → Workspace apps → Notion** (or `NOTION_API_KEY` in
`.env`). **Test connection** calls `GET /v1/users/me` and shows the integration's name and
workspace; nothing changes. The nodes use Notion-Version `2026-03-11` and a database's first
data source (Notion's model since 2025-09-03).

- **Notion: Create Page** (`notion_create_page`): `database_id` (the id, or the database's
  URL), `title` (goes in the database's title property, whatever it's called), `content`: one
  block per line, with `# `, `## `, `### ` headings, `- ` / `* ` bullets, `1. ` numbered items,
  `- [ ] ` / `- [x] ` to-dos, `> ` quotes, and paragraphs for the rest (long bodies are sent in
  batches of 100 blocks). Returns `page_id`, `url`, `blocks`.
- **Notion: Query Database** (`notion_query_database`): `database_id`, an optional
  `filter_property` with `filter_operator` (`equals` or `contains`) and `filter_value`, and
  `max_results` (100, up to 1,000; pages of 100 are followed). The filter is written for the
  property's type: text-like properties (title, text, URL, email, phone) take either operator;
  select and status `equals`; multi-select checks that it contains the value; numbers and
  checkboxes `equals`. Returns `pages`: `[{id, url, created_time, last_edited_time,
  properties}]` with each property as plain JSON (text, a number, a list of names, a date
  `{start, end}`), and `count`.

**Get a Notion integration token and share a database with it:**

1. Open <https://www.notion.so/profile/integrations> (Settings → Connections → Develop or
   manage integrations), **New integration**, pick the workspace, type **Internal**, and save.
2. Under **Configuration**, copy the **Internal Integration Secret** (`ntn_...`). Leave the
   capabilities Read, Update, and Insert content on.
3. An integration sees nothing until it's invited: open the database as a full page, click
   **•••** (top right) → **Connections** → your integration → **Confirm**. Pages under it are
   shared too.
4. The database id is in its URL: `notion.so/<workspace>/<32 characters>?v=...`. Paste the whole
   URL into `database_id` if you like.

A database that isn't shared answers `404`; the node says to add the integration under
Connections.

### Airtable

Connect a token under **Integrations → Workspace apps → Airtable** (or `AIRTABLE_API_KEY`).
**Test connection** calls `GET /v0/meta/whoami` and shows the token's user and scopes.

- **Airtable: Create Record** (`airtable_create_record`): `base_id` (`app...`), `table_name`
  (name or `tbl...` id), `fields` (a JSON object by field name, references allowed, e.g.
  `{"Name": "{{input.name}}", "Score": 7}`), `typecast` (let Airtable convert values, such as
  text to a new select option). Returns `record_id`, `created_time`, `fields`.
- **Airtable: List Records** (`airtable_list_records`): `base_id`, `table_name`, an optional
  `filter_formula` (Airtable formula, e.g. `{Status} = 'Open'` or `FIND('urgent', {Notes})`), an
  optional `view`, and `max_records` (100, up to 1,000; pages of 100 are followed). Returns
  `records`: `[{id, created_time, fields}]`, and `count`.

Errors say what to fix: a rejected token (`401`), a base or table the token can't reach
(`403`/`404`), or a field name or value that doesn't match the table (`422`, with Airtable's
message, e.g. `UNKNOWN_FIELD_NAME`). Airtable allows 5 requests a second per base; a `429` is
retried after the wait.

**Get an Airtable personal access token:**

1. Open <https://airtable.com/create/tokens> → **Create token**, and name it.
2. Scopes: **data.records:read** and **data.records:write** (add `schema.bases:read` only if
   you'll need it elsewhere; these nodes don't).
3. Access: add the bases the token may use (or all current and future bases in a workspace),
   then **Create token** and copy it (`pat...`). It's shown once.
4. The base id is in the base's URL: `airtable.com/appXXXXXXXXXXXXXX/tbl.../viw...`.

**Live tests.** With `NOTION_API_KEY` and `NOTION_TEST_DATABASE_ID`, `pytest -m live` creates a
page titled "FlowForge live test <time>" in that database and finds it again with Query
Database. With `AIRTABLE_API_KEY`, `AIRTABLE_TEST_BASE_ID`, and `AIRTABLE_TEST_TABLE` (and
`AIRTABLE_TEST_FIELD`, a text field, default `Name`), it creates a record and lists it back with
a formula. Without them the tests skip and name the missing variables.

## Knowledge bases (RAG)

![A knowledge base with two documents and a test search](docs/screenshots/knowledge-base.png)

A knowledge base is a named collection of your documents that pipelines search **by meaning**:
"How much does the company pay for my internet?" finds the paragraph about the "40 USD per
month for internet" allowance even though the words differ. **Knowledge** in the top bar
(`/knowledge`) lists yours; open one to add documents, watch them process, and try a search.

**How a document goes in.** Upload a PDF, an image, or a plain-text file (the same type checks
and size limits as `POST /api/files`). The API records it as `pending` and queues
`flowforge.ingest_document` on the **ocr** workers, which:

1. read the text: each PDF page's text layer, OCR (Tesseract) for scanned pages and images,
   or the file as UTF-8 text (shown as *PDF text*, *OCR*, *PDF text + OCR*, or *plain text*);
2. split it into chunks of at most `chunk_size` characters (default 1000), ending at a
   paragraph, line, sentence, or word boundary when one falls in the chunk's second half,
   with `chunk_overlap` characters (default 150) shared between neighbours, starting on a
   word. Each PDF page is chunked on its own, so every chunk can cite one page;
3. embed the chunks in batches of 100 (one request each) and store them in Postgres with
   **pgvector** (`kb_chunks.embedding vector(768)`, an HNSW index on cosine distance);
4. mark the document `ready` (or `failed`, with the reason, and a **Retry** button).

The page polls while anything is `pending` or `processing`. A redelivered task leaves a ready
document alone, and one stuck in `processing` for 30 minutes (its worker died) is taken over.

**Embeddings.** Every knowledge base stores 768-number vectors, whichever model made them:
Gemini (`gemini-embedding-2`, the default; free key) and OpenAI (`text-embedding-3-small`) are
asked for 768 numbers, longer vectors are truncated and re-normalized, and a model that
returns fewer is refused. Ollama's `nomic-embed-text` is 768 natively. Documents and queries
use Gemini's `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY` task types. **Mock** embeds a hashed bag
of words (no key, matches shared words only), for trying it out. A knowledge base keeps the
model it was created with: vectors from different models can't be compared, so the model and
chunking can't change later (create a new knowledge base instead).

**Free-tier limit.** Gemini's free tier embeds 100 texts per minute per project, and every
chunk counts: a 16-page paper is about 75 chunks, so a second document right after it hits the
limit. Ingestion then waits as long as Gemini asks (up to 2 minutes at a time, 5 times per batch;
1 minute inside a pipeline's Add Document) and carries on, so large uploads take longer instead
of failing. Searches don't wait: a rate-limited search fails at once with Gemini's message.

**Nodes** (the **Knowledge** group in the editor):

| Node | What it does | Main outputs |
| ---- | ------------ | ------------ |
| **Knowledge Base: Add Document** (`kb_add_document`) | Reads, chunks, embeds, and stores a file in the knowledge base named in `knowledge_base` (its name or id), inside the run, on the ocr queue | `document_id`, `status` (`ready`), `chunk_count`, `char_count`, `pages`, `method` |
| **Chunker** (`chunker`) | Splits any text (`chunk_size`, `chunk_overlap`) | `chunks` (`index`, `text`, `start`, `end`), `count` |
| **Embedding** (`embedding`) | Embeds a text or a list of texts (`provider`, `dimensions`, `task`) | `embedding` (one text), `embeddings`, `dimensions` |
| **Retriever** (`retriever`) | The `top_k` chunks closest to `query`, optionally above `min_score` (cosine, -1 to 1) | `results` (`rank`, `citation` "[n]", `score`, `content`, `filename`, `page`, `chunk_id`, `document_id`), `context` (numbered sources for a prompt) |
| **Reranker** (`reranker`) | An LLM scores each result 0-10 against the question (JSON, validated, one retry); keeps the best `top_n`, renumbered | `results` (plus `rerank_score`, `retrieval_rank`), `context`, `reranked` |

If the reranker's reply can't be used (or the provider fails), it keeps the retrieval order
and says so (`reranked: false`, `warning`); turn off `keep_order_on_failure` to fail instead.

**Templates.** **PDF to Knowledge Base** (file → Add Document → Output) and **Document Q&A**
(question → Retriever (8) → Reranker (4) → LLM answer citing `[n]` with a `Sources:` list of
file and page → Output with the answer and the sources). Both read the knowledge base name
from the `knowledge_base` variable (default **My documents**), which **Use template** creates
if you don't have it, embedded with Gemini (or OpenAI, or mock without either).

**Following a citation.** Each source in the answer carries its `chunk_id`;
`GET /api/knowledge-bases/{id}/chunks/{chunk_id}` returns that chunk's text, file, page, and
character offsets in the page (or file) it came from.

**Existing installs: switching the Postgres image.** The stack now uses
`pgvector/pgvector:pg16` instead of `postgres:16-alpine`. Same Postgres major version, so the
data volume works as it is, but the new image sorts text with glibc instead of musl. After the
first start on an existing volume, rebuild the indexes once:

```bash
docker compose up -d postgres
docker compose exec postgres psql -U flowforge -d flowforge -c "REINDEX DATABASE flowforge;"
```

Then `docker compose up -d --build` (the API applies the knowledge-base migration on start).
A fresh volume (`docker compose down -v`) needs nothing.

## Privacy and security

![The Privacy Report on a run](docs/screenshots/privacy-report.png)

FlowForge looks for secrets and personal data in what pipelines send out, and keeps it out
of run history. One detection service ([`flowforge_engine/privacy.py`](packages/workflow-engine/src/flowforge_engine/privacy.py))
does all of it; the guard, the nodes, and the masking of stored data call into it.

**What it finds.** Findings are a type, a category, a field path and character offsets, a
confidence, and the detector; never the matched text.

| Category | Types | How |
| -------- | ----- | --- |
| secret | AWS, Google, GitHub, Stripe, Slack, OpenAI, Anthropic, Groq keys; Telegram bot tokens; JWTs; private-key blocks; `password=` / `api_key:` style assignments; the password in `postgres://user:pass@host` style URLs; high-entropy tokens of 20+ characters right after "key", "token", "secret", "password", or "bearer" | Patterns; entropy ≥ 3.5 bits with 3+ character classes. UUIDs and hex digests (commit SHAs, checksums) are never flagged |
| financial | Card numbers | 13-19 digits that pass the **Luhn** check and start like a card network |
| government_id | **Aadhaar** (validated with the **Verhoeff** checksum), **PAN** | A PAN's fourth letter must be a holder type (P, C, H, F, A, T, B, L, J, G); PANs have no public check digit |
| personal | Emails, Indian phone numbers (+91 / 0 / 10 digits from 6-9) by rule; names, locations, other phone numbers, IP addresses, IBANs from **Microsoft Presidio** with spaCy's `en_core_web_sm` | Only when asked: the workflow's "detect personal data" setting, or the node's own option |

Overlapping matches keep the most specific one (a secret over an ID over a card number over
personal data). A per-workflow **allowlist** suppresses known-safe values: an entry matches text
exactly (ignoring case), or is a regex written `re:<pattern>`.

**The privacy guard.** Every node that sends data out (Gmail, Telegram, Discord Webhook, HTTP
Request, Notion Create Page, Airtable Create Record) and every LLM node (the nine providers,
Structured Output, Summarize, Entity Extraction, Vision, Reranker) has a `privacy_guard` setting,
applied by the executor after `{{...}}` is resolved and before the node runs. It only checks the
node's content fields (a mail's subject, body, and attachments; a prompt; an HTTP URL, query, and
body), not recipients, ids, or auth headers.

| Mode | What happens | Default for |
| ---- | ------------ | ----------- |
| `block` | The node fails with what was found, by type and count: "Blocked by the privacy guard: found 1 card number, 1 Aadhaar number in body". Nothing is sent | outbound nodes |
| `redact` | It runs with each finding replaced by `[REDACTED:<TYPE>]` | LLM nodes |
| `warn` | It runs unchanged; the step records a warning | |
| `off` | Nothing is checked | |

The step's input in the run history is what the node actually ran with (the redacted version,
in redact mode), and its `privacy` record says what the guard did.

**Masking stored data** (per workflow, **on by default**, in the editor's **Privacy** panel).
Step inputs and outputs in `node_executions`, live WebSocket events, the run's inputs, and its
final output are stored with every finding replaced by `[REDACTED:<TYPE>]`. The next steps still
get the real values: a run that continues on another queue reads earlier steps' real outputs from
an encrypted Redis hash (Fernet, the credentials key), deleted when the run finishes. The run's
inputs are masked once its first segment has read them. Turn masking off to debug with the real
values in the history.

**The Privacy Report** (on each run's page, and `privacy_report` in `GET /api/executions/{id}`):
counts by category and type, each step that held something, and what the guard did there. No
values are stored for it or shown. A value counts once per step (the same card under two keys is
one), and the run's totals take, per type, the most any one step held, so a value passed from step
to step counts once.

**Nodes** (the **Privacy** group):

| Node | Does | Outputs |
| ---- | ---- | ------- |
| **Secret Scanner** | Scans text or any JSON (`data`); `include_personal_data` (default on); `fail_on_findings` to stop the run | `findings` (type, category, path, start, end, confidence, detector), `count`, `clean`, `by_type`, `by_category` |
| **PII Redact** | Replaces each distinct value with `<TYPE_n>` (the same value, the same placeholder), so an LLM never sees it | `text`, `mapping_id`, `placeholders`, `by_type` |
| **PII Restore** | Puts the originals back in `text` (e.g. the LLM's answer) | `text`, `restored`, `unknown_placeholders` |
| **Redact Image** | Covers what's sensitive in a screenshot or photo with black boxes: Tesseract reads each word with its position (small images at 1600 px wide), each line is scanned, and every word a finding overlaps is covered (`padding` px around it). On the ocr queue | `file` (a PNG attachment, base64), `summary` ("1 card number, 1 PAN"), `count`, `hidden_words`, `by_type` |

Redact keeps its `{placeholder: original}` mapping in Redis, Fernet-encrypted, for **one hour**,
keyed to the execution (another run can't read it), so Restore works after the run moved to
another queue and worker. Its output carries only the `mapping_id`.

**ICS Calendar Event** (`ics_calendar`). Makes an `.ics` file with the `icalendar` library from a
title, a start, an end (or `duration_minutes`), a location, a description, attendees, and an
optional organizer: a stable UID (a hash of the title and start, so sending it again updates the
event), a reminder `alarm_minutes` before (default 30), and `METHOD:PUBLISH` (or `REQUEST` for an
invitation). Times without an offset are in `timezone` (default `Asia/Kolkata`). Dates must be
explicit ISO 8601 (`2026-10-05T15:00`, or `2026-10-05` for all day): anything else, like
"tomorrow evening", succeeds with `ambiguous: true` and a `reason`, and no file, rather than
guessing. `{{ics.attachment}}` drops into a Gmail node's `attachments` or a Telegram node's
`document`. Put `{{system.now}}` (the run's time, UTC ISO 8601) or `{{system.today}}` in the
extraction prompt so an LLM can turn "tomorrow at 3" into a date.

**Telegram documents.** The Telegram node sends a file as well as text: `document` (inline
content such as `{{ics.attachment}}`, text or base64) or `document_file` (an upload). The text
becomes its caption, or a message before it when longer than 1,024 characters. Up to 50 MB.

File attachments with `encoding: base64` (a Redact Image result, a Gmail attachment) are never
scanned or rewritten: random base64 can look like a PAN, and masking it would corrupt the file.
Presidio's name and location matches without a capital letter are dropped (it tags words like
"docker" as people), and a labelled field ("Name: Priya Deshmukh", "Full name –") counts as a name
by rule, since the small model often misses those.

**Limits.** Streaming LLM tokens are shown live as they arrive, unmasked (they're never stored).
For Each's per-item prompts aren't guarded (they're resolved per item inside the node). Presidio's
small English model misses some names and finds some false ones; personal-data detection is off
by default for that reason. PANs are checked by shape, not a checksum.

## Telegram Command Center

Run your deployed pipelines by messaging your Telegram bot: "what's new in pgvector?", a voice
note saying "email me a reminder to call the bank", or a photo of a receipt.

**Setup.**
1. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` (see [Create a Telegram bot](#create-a-telegram-bot-and-find-your-chat-id)).
   The `telegram-listener` service (started by `docker compose up`) long-polls that bot; without a
   token it idles. Run exactly one: Telegram allows one `getUpdates` consumer per bot, and the
   listener removes any webhook set on the bot when it starts.
2. **Deploy** each pipeline you want to reach, with a **description** of what it does (now
   required on the first deploy): the router matches messages against it.
3. In the editor's **Triggers** panel, switch on **Telegram message**. Its allowlist of chat ids
   defaults to `TELEGRAM_CHAT_ID`; add others (comma-separated; groups are negative numbers).

**How a message is handled** ([`app/services/telegram_center.py`](apps/api/app/services/telegram_center.py)):

1. **Once per update.** Each `update_id` is claimed with an atomic Redis `SET NX` (kept 7 days)
   before anything happens, and the listener saves the next offset in Redis after each update, so a
   restart, a redelivered batch, or a second listener never runs anything twice.
2. **Allowlist.** Only chats on the allowlist of some workflow's enabled Telegram trigger whose
   pipeline is deployed count. Everything else is ignored without a reply, and logged with the chat
   id only (never the text).
3. **Rate limit.** `TELEGRAM_RATE_LIMIT_PER_MINUTE` (10) requests per chat per minute, in Redis;
   the first one over gets one "slow down" reply, the rest are dropped.
4. **Media.** A voice note (or audio) is downloaded, stored as an upload, and transcribed by the
   Speech to Text node (Groq's Whisper with a key, faster-whisper on the CPU without); the bot
   replies "I heard: …" and routes the transcript like a typed message. A photo (the largest size)
   or a document is stored as an upload and fills the chosen pipeline's file input.
5. **Privacy.** A message holding a secret, a card number, an Aadhaar or a PAN runs nothing ("I
   didn't run anything: your message contains 1 card number"). Every reply goes out through the
   privacy layer's masking, and the run follows the pipeline's own guard and masking settings.
6. **Intent router.** An LLM (the owner's Gemini, then Groq, then OpenRouter) gets the message and
   each candidate's name, description, and inputs, and answers JSON: the pipeline, the inputs it
   could fill from the message, a confidence, and what's missing. The answer is checked rather than
   trusted: inputs the pipeline doesn't have are dropped, and a required input that isn't filled, a
   missing file, or confidence under 0.6 means **one clarifying question** ("Who should I greet?").
   The reply is routed together with the first message; if it's still unclear, the bot lists what
   it can run. Without a working LLM, a chat with one simple pipeline (at most one text input) still
   works: the whole message becomes that input.
7. **Confirmation for side effects.** A deployment records `side_effects` when its graph sends or
   writes somewhere: Gmail, Telegram, Discord, Notion Create Page, Airtable Create Record, Knowledge
   Base: Add Document, HTTP Request with a method other than GET/HEAD, or Gmail Read with "mark as
   read". Those runs wait for a tap on **✅ Confirm** / **✖ Cancel** under a summary of the inputs.
   Each button's data is `c|x:<id>:<expiry>:<signature>` (under Telegram's 64 bytes): an HMAC of the
   action, the request id, the chat, and the expiry, with the server's `JWT_SECRET`. A tap in another
   chat, a changed token, or a tap after `TELEGRAM_CONFIRM_SECONDS` (300: 5 minutes) is rejected with
   a clear message ("This confirmation expired… Send the request again"). The request itself waits
   in Redis, Fernet-encrypted, and can be confirmed once.
8. **Running.** "⏳ Running <name>…", then the deployment runs exactly as through its API (its graph
   snapshot, the owner's credentials, the workflow's runs-per-hour limit), recorded with trigger
   **telegram**. The bot waits up to `TELEGRAM_RUN_WAIT_SECONDS` (600) and replies with the final
   output (a single text output as is, anything else as JSON, cut at 3,500 characters; "Done." when
   the pipeline has no Output node), or "❌ <name> failed: <error>" with the run id. A file in the
   output (`{filename, content, encoding, content_type}`, e.g. an ICS node's `{{ics.attachment}}`)
   is sent as a Telegram document, with the text as its caption. Three templates are made for this:
   **Paper Digest**, **Safe to Share**, and **Calendar Invite** (deploy each with a description).

## Discord voice meetings

Two services watch **one voice channel in one server**: `discord-recorder`
([`apps/discord-recorder`](apps/discord-recorder), discord.js: it sits in the channel and does the
recording, with Discord's end-to-end encrypted voice handled by `@discordjs/voice`) and `discord-bot`
([`app/discord_controller.py`](apps/api/app/discord_controller.py): it decides). They talk through two Redis
lists. Nothing is recorded on its own: it asks you in Telegram first, tells everyone in the channel when it
records, and sends the recording (or notes) back to the same Telegram chat.

**Set it up**

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** →
   **Bot** → **Reset Token**; put it in `.env` as `DISCORD_BOT_TOKEN`. No privileged intents are needed.
2. **OAuth2 → URL Generator**: scope `bot`; bot permissions **View Channels**, **Connect**, **Speak**,
   and **Send Messages** (the channel's text chat). Open the generated URL and add the bot to your server.
   Make sure the bot's role can see and join the channel (a private channel needs an explicit grant).
3. In Discord, **User Settings → Advanced → Developer Mode**; right-click the server and the voice
   channel → **Copy ID** into `DISCORD_MONITOR_GUILD_ID` and `DISCORD_MONITOR_CHANNEL_ID`.
4. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be set (the prompt goes to that chat) and the
   `telegram-listener` must be running: it receives the Yes/No tap.
5. Create the **Meeting Notes** workflow from its template (the bot looks for a deployed pipeline, else
   a workflow, named `DISCORD_MEETING_PIPELINE`, default "Meeting Notes").
6. `docker compose up -d discord-recorder discord-bot`. Run exactly one of each. Without the token, guild, and
   channel they idle. (`DISCORD_RECORDER=pycord` makes `discord-bot` record by itself with py-cord instead;
   py-cord 2.8's voice receive is unreliable, so that is only a fallback.)
   Optional: `DISCORD_RECORDING_MAX_MINUTES` (90), `DISCORD_MIN_RECORDING_SECONDS` (30).

**How it works, end to end**

1. **Detect.** When the channel goes from empty to non-empty, a *session* begins (bots don't count). People
   already in the channel when the service starts are an ongoing session it isn't asked about.
2. **Ask.** The bot does *not* join. It sends the Telegram chat "Voice activity detected in #channel — record
   and summarize this meeting?" with **Yes** and **No** buttons. They are the Command Center's signed
   buttons: an HMAC token bound to that chat and an expiry `TELEGRAM_CONFIRM_SECONDS` (300, 5 minutes)
   away, usable once; a tap from another chat, a forged or altered token, or a late tap is refused.
3. **Answer.** The `telegram-listener` (the only `getUpdates` consumer) records the tap in Redis and the
   bot picks it up.
   - **No**, or **no answer in 5 minutes**: nothing is recorded, and that session isn't asked about again,
     however long people keep talking. The next time the channel goes from empty to non-empty (a separate
     session) it asks again.
   - **Yes**: the bot joins, posts **"🔴 Recording started for meeting notes, per request"** in the
     channel's text chat, and records everyone into one mixed mono 16 kHz WAV. If it can't post the notice
     it doesn't record and says so in Telegram.
4. **Stop.** When the channel empties, or at `DISCORD_RECORDING_MAX_MINUTES` (a capped session isn't re-asked
   either). A recording under 30 seconds isn't processed (you get a one-line note), and a recording that
   captured no audio at all is reported as such.
5. **Deliver (`DISCORD_OUTPUT=audio`, the default).** The bot sends the recording to the Telegram chat as an
   MP3 (64 kbps, so 90 minutes fit Telegram's 50 MB limit; a copy is kept in your uploads). Right after it comes
   **"Want notes for this recording?"** with **📝 Summary**, **📄 Full transcript**, **Both**, and **✖ No thanks**
   buttons (the same signed, chat-bound, 5-minute buttons). Only when you tap one does the **Meeting Notes**
   pipeline run on that recording (trigger **discord voice**): you get the summary (decisions and action items)
   as a message, the whole-meeting transcript as a `.txt` file, or both. Nothing is transcribed or summarized
   unless you ask. Set `DISCORD_OUTPUT=notes` to skip the audio and run steps 5-6 below straight away.
5. **Process (`notes`).** The WAV is stored like an upload and the **Meeting Notes** pipeline (Speech to Text →
   summary, decisions, action items) starts on it right away, recorded as a run with trigger
   **discord voice** in Executions. Its Telegram nodes are muted: the bot delivers the result itself.
6. **Choose.** While that runs, the chat gets "What do you want?" with **📝 Summary**, **📄 Full transcript**,
   and **Both** buttons (the same signed, chat-bound, 5-minute buttons). When the run finishes you get what
   you picked: the summary as a message, the whole-meeting transcript as a `.txt` document. No answer in
   5 minutes sends the summary. If transcription or summarization fails, the chat gets
   "❌ The meeting summary failed: …" with the run id. Everything sent is masked by the privacy layer.

State is in memory: restarting `discord-bot` forgets the current session (the recorder re-sends who is in the
channel when it connects, and people already there aren't asked about). The recorder mixes everyone into one mono
16 kHz WAV on the shared files volume; the controller smooths it (short holes from lost packets bridged, edges
faded, level raised) and converts it to MP3 before sending. Tests: `docker compose exec api pytest
tests/test_discord_controller.py tests/test_discord_voice.py`, and the mixer's with `npm test` in
`apps/discord-recorder`.

## Multi-agent resume refinement

Upload a resume PDF (and, optionally, paste a job description) on the **Resume** page. Five specialist
agents run one after another, each its own LLM call with its own role, schema, and checks, never one
mega-prompt. The page shows what each agent found in an expandable section, then the improved draft
with a before/after diff for every bullet.

```
PDF ─► Extract ─► Parser ─┬─► ATS Compatibility ─┐
       (text +            ├─► Content & Impact ──┼─► Rewrite ─► refined PDF + DOCX ─► email to yourself
        layout)           └─► Job-Match (if JD) ─┘
```

| Agent | Input | What it returns |
|---|---|---|
| **Extract** (no LLM) | the PDF | text via the **PDF Extract** node, plus *measured* layout facts from PyMuPDF: text columns, tables, images and icons, text in the page header/footer, section headers |
| **Parser Agent** | the text | `{contact_info, summary, experience[{company, title, dates, bullets[]}], education[], skills[]}`; its bullets must appear in the resume text (it restructures, never invents) |
| **ATS Compatibility Agent** | parsed structure + the measured layout | a score and a list of issues, each a `blocker` or `warning` with a plain-language explanation, the evidence, and a fix: multi-column, tables, images/icons instead of text, header/footer text, non-standard section names (a "My Journey" instead of Experience), missing sections, contact/date problems. Measured blockers the model overlooks are added to its list (marked *measured in the PDF*) |
| **Content & Impact Agent** | every bullet, with an id | for each bullet: flags (`weak_opening_verb`, `no_metric`, `vague_claim`, `passive_voice`), a one-line explanation of *that* bullet, and a specific rewrite; plus a review of the summary |
| **Job-Match Agent** (only with a job description) | parsed resume + the job description | matched and missing keywords (missing ones ranked high/medium/low, with an honest suggestion or "true gap"), and for each requirement the existing bullets that answer it best |
| **Rewrite Agent** | everything above | the full improved draft: same jobs in the same order, every original bullet exactly once, rewritten per the Content Agent and (with a job description) ordered and emphasised per the Job-Match Agent |

**Why a dedicated service rather than a template:** the feature needs stored per-agent output, versions of
the same resume, downloads, and an email history, none of which a workflow run keeps in that shape. It
still reuses the engine: PDF text comes from the **PDF Extract** node, and every agent asks through the
engine's provider chain (`generate_with_fallback`: Gemini, then Groq, then OpenRouter, whichever you have keys
for), so the usual fallback applies.

**Every stage is stored, not just the result.** Each agent's row keeps the model that answered, the
instructions it was given, *every raw reply* (with the problems that made a reply be retried), its validated
output, and its timing, and the UI shows them under "The agent's raw output". The run executes on a Celery
`llm` worker; the page polls and fills in as each agent finishes.

**Guardrails.** A model's JSON can be valid and still wrong, so each reply is checked beyond its schema, with
one or two retries that list the problems (`app/services/resume_agents.py`):

- the Parser's bullets must come from the resume text; the Content Agent must review every bullet id; the
  Job-Match Agent may only cite real bullet ids; the Rewrite Agent must keep the structure, use every original
  bullet exactly once, and add no skills the resume doesn't list;
- no invented numbers: a metric in a rewrite must already be in the original bullet, otherwise it is a
  `[placeholder]` such as `[X%]` for you to fill in (the draft tells you how many there are);
- no padding: a rewrite that adds explanatory clauses the original doesn't state is sent back;
- past roles use the past tense, the current role the present tense;
- after the last attempt the assembler corrects what is left deterministically (it falls back to the Content
  Agent's clean suggestion, or to the original, keeps bullets the Content Agent judged fine word for word
  unless a job description asks for tailoring, and restores contact details, companies, titles and dates from
  the parsed resume). Each correction is listed under "automatic corrections".

**Downloads and email.** `GET /api/resume-refinements/{id}/download?format=pdf|docx` builds the refined draft
as a single-column, plain-text PDF (reportlab) and a DOCX with real headings and bullets (python-docx); the
generated PDF passes this feature's own ATS layout checks. "Email me the refined resume" sends both files
through your Gmail SMTP integration with a short summary ("Rewrote 6 of 9 bullets, fixed 5 ATS formatting
issues, matched 8 of 11 job-description keywords…"). **The endpoint takes no recipient**: it sends only to
the logged-in account's own address, so an auto-rewritten resume is reviewed by you before it goes to anyone
else. Each send is recorded (who, when, which version, the summary, the files) and listed under the
refinement; refining the same file again creates the next version.

API: `POST /api/resume-refinements {file_id, job_description}` (upload the PDF with `POST /api/files` first;
202, then poll `GET /api/resume-refinements/{id}`), `GET` list, `DELETE`, `/download`, and `/email`.

**Verified on real runs.** [`docs/resume-verification`](docs/resume-verification) holds every agent's actual
output and the final draft for three sample resumes ([`samples/resumes`](samples/resumes)), with and without
a job description: a clean backend engineer (Priya), an ATS-hostile designer resume (two columns, a skills table,
icons, creative headers, contact only in the page header), and a thin graduate resume with no summary or skills
section. It also holds the PDF and DOCX that were emailed to a real inbox in the end-to-end check. Tests:
`docker compose exec api pytest tests/test_resume_refinement.py` (schemas, checks, layout, guardrails,
documents, and the API flow with a scripted LLM) and `npm test` in `apps/web` for the diff.

## Generate with AI, Run Replay, and the command palette

**Generate with AI** (Dashboard → **Generate with AI**, or `POST /api/workflows/generate {prompt}`).
Describe a pipeline in plain English ("summarize my unread emails and Telegram me the summary").
An LLM (your Gemini, then Groq, then OpenRouter) gets a compact catalog of every node type built
from the registry (the same schemas as `GET /api/nodes`: config fields with types, required ones,
defaults; output keys; the fields of list items such as `emails[]`), the `{{node.key}}` reference
rules, and your request, and answers a graph as JSON. FlowForge then checks it rather than
trusting it ([`app/services/generation.py`](apps/api/app/services/generation.py)): the JSON must
parse; every node type must exist; edges must connect declared nodes; the workflow validator must
pass (config schemas, references to upstream nodes, condition branches). LLM steps are pointed at
the providers you have keys for, file inputs lose any invented default, and the canvas is laid out
in columns. Any problem goes back to the model with the exact messages and its previous answer,
up to **3 attempts**. Only a graph that validates is saved (as a draft) and opened in the editor;
otherwise the dialog lists what was still wrong and nothing is saved. Providers that aren't
connected come back as warnings. Verified with real Gemini: "summarize my unread emails and
Telegram me", "extract the entities from an uploaded invoice", and "search the web for news about
pgvector and email me a summary" each validated on the first attempt and ran successfully.

**Run Replay** (**Replay** on a finished run's page). The run is rebuilt from what was already
stored, with no new recording: each node row's `started_at` / `finished_at` / status, and the graph
snapshot the execution keeps (now included in `GET /api/executions/{id}` as `graph`, the graph
exactly as it ran, even if the pipeline was edited since). Every step becomes an offset from the
run's start, so queue waits and hand-offs between workers keep their real length (the Meeting Notes
run shows a 228 ms gap between the audio and llm workers). The canvas uses the live run's colors
(gray, blue while running, green, red; edges animate into the running node) with play / pause, a
1 ms scrub bar, speeds 0.5×–10×, and a list of start/finish events you can click to jump to
([`replay-model.ts`](apps/web/src/features/executions/replay-model.ts)).

**Command palette** (**Ctrl+K** / **⌘K** anywhere, the editor included). Fuzzy-searches your
pipelines, the pages (Dashboard, Executions, Knowledge bases, Integrations), and quick actions:
Create pipeline, Generate pipeline with AI, and, in the editor, **Run current pipeline** (as the
Run button: it asks for inputs if the pipeline has any) and **Deploy current pipeline**. Characters
match in order ("zebr" finds "Palette zebra"), ranked by exact substrings, word starts, and
consecutive letters; keywords match too ("rag" finds Knowledge bases, "settings" Integrations).
↑/↓ and Enter to choose; Escape or a click outside closes it.

## Notifications: Telegram and Discord

Both split messages over the service's limit into several (Telegram 4,096 characters, Discord
2,000), honor `429` rate limits (Telegram's `parameters.retry_after`, Discord's `retry_after`
or `Retry-After`) by waiting that long and retrying, and can't be interrupted mid-send, so it's
never unclear whether a message went out. Secrets (the bot token, the webhook URL) are
redacted from every error.

- **Telegram** (`telegram`): `text`, `chat_id` (blank = the integration's default chat),
  `format`: `markdown` (default: the Markdown LLMs write, with bold, italics, headings, lists,
  links, and code, becomes Telegram's HTML, with everything else escaped; if Telegram still
  rejects the markup, the words are sent as plain text), `html`, or `text`. Also
  `disable_link_preview` and `silent`.
- **Discord Webhook** (`discord_webhook`): `content` (Discord Markdown), an optional embed
  (`embed_title`, `embed_description`, `embed_url`, `embed_color` like `#5865F2`,
  `embed_footer`), `username`, `avatar_url`, `thread_id`. `@everyone` and other mentions don't
  ping unless `allow_mentions`. The webhook comes from the Discord integration, or
  `webhook_url` on the node (it must be a `discord.com/api/webhooks/...` URL).

Credentials go under **Integrations → Notifications**, encrypted like the others, or in `.env`
as server-wide defaults (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`). A
user's own Telegram credential replaces the server's entirely, chat included. **Test
connection** makes real calls that send nothing: Telegram `getMe` (the token works) and
`getChat` on the default chat (the bot can reach it); Discord a `GET` of the webhook.

### Create a Telegram bot and find your chat id

1. In Telegram, open a chat with **@BotFather**, send `/newbot`, and pick a display name and a
   username ending in `bot`. BotFather replies with the token (`123456789:AAE...`). Treat it
   as a password.
2. Open your new bot (the `t.me/<username>` link BotFather gives you) and press **Start**, or
   send it any message. A bot can't message you first.
3. Open `https://api.telegram.org/bot<token>/getUpdates` in a browser. Your message is in
   `result[0].message`, and `message.chat.id` is your chat id (a number such as `858327745`).
   For a group, add the bot to the group, send a message there, and use that chat's id (it
   starts with `-`).
4. Put them in `.env` (`TELEGRAM_BOT_TOKEN=...`, `TELEGRAM_CHAT_ID=...`, then
   `docker compose up -d` to recreate the containers), or paste them under Integrations →
   Telegram bot. Press **Test connection**: it should name your bot and your chat.

### Create a Discord webhook

1. In Discord, open the server's channel settings (the gear next to the channel) → **Integrations** →
   **Webhooks** → **New Webhook**. You need the Manage Webhooks permission.
2. Name it, optionally pick an avatar, and press **Copy Webhook URL**
   (`https://discord.com/api/webhooks/<id>/<token>`). Anyone with it can post to the channel.
3. Paste it under Integrations → Discord webhook (or `DISCORD_WEBHOOK_URL` in `.env`) and
   press **Test connection**.

## Templates

![Templates on the dashboard](docs/screenshots/templates.png)

The dashboard's **Templates** section lists eleven ready-made pipelines. Each card shows its
steps and the credentials it needs, with a check or a warning for each and the provider that
would be used. **Use template** creates an editable copy you own and opens it in the editor.
LLM steps use the first free provider you have a key for (Gemini, then Groq, then OpenRouter),
with the others as fallbacks. A Telegram step becomes a Discord Webhook step if you only have
Discord, or a Gmail step to your own address if you only have Gmail. Speech to Text uses Groq
with a Groq key and local faster-whisper without one. The template's trigger is created switched off, with a schedule in your browser's
time zone. The catalog lives in [`app/services/templates.py`](apps/api/app/services/templates.py)
and is written to the `templates` table on API start and by the seed
(`GET /api/templates`, `POST /api/templates/{slug}/use`).

| Template | Steps | Needs |
| -------- | ----- | ----- |
| **Morning Digest** | Schedule (07:30) → Gmail Read (unread, last day) + RSS Feed (BBC Technology, new since the last digest) → For Each (one-line summary each, 2 at a time, 10/min) → Join → Gemini (the digest, in Markdown) → Telegram → Output | Gmail App Password, an LLM key, Telegram (or Discord) |
| **Invoice Extractor** | Input (file; defaults to the sample scan) → OCR (the text layer where a PDF page has one, Tesseract for scanned pages) → Entity Extraction (parties, dates, amounts, invoice number, vendor, customer, due date, total due) → Output (JSON). **Download JSON / CSV** on the run: the CSV has one row per entity | An LLM key |
| **Email Triage** | New email → Gemini (urgent / normal / spam, temperature 0) → Condition (contains "urgent", ignoring case) → Telegram only for urgent → Output. The Input has a sample urgent email for manual runs | Gmail App Password, an LLM key, Telegram (or Discord) |
| **Meeting Notes** | Input (audio/video file; defaults to [`samples/team-meeting.mp3`](samples/team-meeting.mp3), or record in the Run form) → Speech to Text (Groq Whisper, vocabulary prompt from `{{vars.vocabulary}}`) → Structured Output (`summary`, `decisions[]`, `action_items[{task, owner, due}]`, validated against a JSON Schema, one retry with the problems if the reply doesn't match; `owner` "unassigned" and `due` "not set" when not said) → Join ×2 → Telegram (or Discord, or Gmail) → Output (the notes, the transcript, and the timestamped segments) | Groq key (or local faster-whisper, no key), an LLM key, Telegram / Discord / Gmail |
| **Web Research** | Input (a question) → Web Search (DuckDuckGo, 5 results, Tavily fallback) → Web Page (the top result's text; a site that blocks readers gives empty text instead of failing) → Join (numbered sources) → Gemini (answers only from the sources, citing `[1]`, `[2]`, ... and ending with a `Sources:` list of `[n] Title - URL`) → Output (the answer and the sources) | An LLM key (Tavily optional) |
| **PDF to Knowledge Base** | Input (a PDF, image, or text file) → Knowledge Base: Add Document (into `{{vars.knowledge_base}}`, default "My documents", created by **Use template** if missing) → Output (status, chunks, how it was read) | Gemini (or OpenAI) for embeddings; mock without |
| **Document Q&A** | Input (a question) → Retriever (8 closest chunks) → Reranker (an LLM keeps the best 4) → Gemini (answers only from them, citing `[1]`, `[2]`, ... and ending with `Sources:` as `[n] file, page`) → Output (the answer and the sources, each with its `chunk_id`) | Gemini (or OpenAI) for embeddings, an LLM key |
| **Paper Digest** | Input (a paper PDF) → PDF Extract → Structured Output (title, problem, contributions, data, results with numbers, limitations) and Knowledge Base: Add Document (`{{vars.knowledge_base}}`) → Join ×3 → Text → Output (the digest). Ask Document Q&A about it afterwards | Gemini (or OpenAI) for embeddings, an LLM key |
| **Safe to Share** | Input (a screenshot or photo) → **Redact Image** (black boxes over each sensitive word) and, as a second opinion, Vision → Secret Scanner → Condition (did Vision see more than was covered?) → Output `{message, file}`: "🛡️ Covered: 1 database password, 1 AWS access key…" with the redacted picture, or a "check it before posting" warning. From Telegram, the picture comes back as a photo | A Gemini key (Vision) |
| **Calendar Invite** | Input ("GATE mock test next Sunday 10 to 1") → Structured Output (exact ISO times from `{{system.now}}` in `{{vars.timezone}}`, Asia/Kolkata) → ICS Calendar Event → Condition (date clear?) → Output `{message, file}`, or a question when the date is vague. From Telegram, the `.ics` arrives as a document | An LLM key |
| **Job Alert Filter** | Schedule (every 6 h) → RSS Feed (We Work Remotely, programming; new since the last run) → For Each (score 0-100 against `{{vars.resume}}`, as JSON) → Filter (`output.score` ≥ `{{vars.threshold}}`, 70) → Condition (any?) → Join → Telegram → Output | An LLM key, Telegram (or Discord) |

Edit the variables (`feed_url`, `resume`, `threshold`) in the **Variables** panel. **Download
JSON / CSV** is on every run with a final output (the run panel and the execution page;
`GET /api/executions/{id}/output?format=csv`): the CSV turns any JSON into rows, one per object
in any list, with a `group` column saying where each row came from.

## Free-tier rate limits

Free tiers change without notice, so nothing in FlowForge hardcodes a quota; these are the
levers:

- **For Each** is where calls multiply: keep `rate_limit_per_minute` under your provider's
  free-tier requests-per-minute limit (check its rate-limits page; the templates use 10) and
  `concurrency` low (2). A Morning Digest of 18
  items took 67 s, 53 of them spent waiting on the limit. The limit applies per node per run:
  two pipelines running at once each get their own budget.
- **Fallbacks:** every LLM step can fall back to another provider (templates add the ones you
  have keys for), so a `429` or an overloaded model moves to the next one instead of failing
  the item.
- **Retries:** `429`s and `5xx`s from LLMs, Telegram, and Discord are retried with backoff,
  honoring the service's `Retry-After`, up to `LLM_MAX_RETRIES` and waiting at most
  `LLM_RETRY_MAX_DELAY_SECONDS` (a longer requested wait gives up at once, so a fallback can
  answer).
- **Triggers:** `max_runs_per_hour` caps how often a schedule, the inbox, or webhook callers
  can start a pipeline, and a trigger that keeps failing (for example on an exhausted daily
  quota) switches itself off after `max_consecutive_failures` instead of burning the quota
  all day.
- **Daily quotas:** Gemini's free tier also has requests-per-day limits per model. An
  every-minute schedule on an LLM pipeline uses them up quickly, so prefer hourly or daily
  crons for LLM work, and `GEMINI_MODEL` for a model with more free quota.
- **Groq Whisper** (free, when checked): 20 requests a minute, 2,000 a day, 7,200 audio
  seconds an hour, and 28,800 a day, 25 MB per file. So about 2 hours of audio per hour: a
  1-hour meeting is 6 chunks of 10 minutes and one-sixth of the daily audio. A `429` names
  these limits; switch the node to `local` (no quota, slower) for bulk work. Details:
  <https://console.groq.com/docs/speech-to-text> and <https://console.groq.com/docs/rate-limits>.
- **faster-whisper** (local) has no quota: it's bound by the worker's CPU. On this machine
  `base` transcribed the 100-second sample in 6.9 s; `small` and larger are more accurate and
  several times slower.
- **DuckDuckGo** has no documented quota but rate limits bursts (the error says so). Set
  `TAVILY_API_KEY` (free: 1,000 credits a month, 1 per basic search) and searches fall back to
  Tavily automatically.
- **Mistral**'s free "Experiment" plan needs phone verification and has low rate limits;
  **Cerebras**'s free tier allowed 5 requests a minute and 1M tokens a day when checked. Keep
  For Each's `rate_limit_per_minute` at or below those.
- **Telegram** asks bots to send at most about one message per second to a chat, and
  **Discord** rate-limits each webhook. Both answer `429` with the time to wait, which the
  nodes honor.

## Tests

### Backend

One `pytest` run covers the API suite (`apps/api/tests`) and the engine suite
(`packages/workflow-engine/tests`):

```bash
docker compose exec api pytest                     # inside the running stack
docker compose exec api pytest -m "not network"    # skip the one test that calls httpbin.org
docker compose exec api pytest -m live -s          # real providers (see below)
```

API tests run against a real Postgres: a `flowforge_test` database is dropped, recreated, and
migrated with Alembic at the start of each run, and every test is rolled back afterwards. The
dev database is never touched. The suite sets `TESTING=true`, which is the only thing (besides
a node explicitly choosing provider `mock`) that makes the provider factory hand out mocks, so
unit tests never call real APIs even when real keys are in `.env`. `test_migrations.py` also
round-trips every migration (upgrade → downgrade → upgrade) on a scratch database and checks it
matches the models.

**Live tests** (`@pytest.mark.live`) call the real services with the keys in `.env` and are
skipped unless you select them with `-m live`. Each one skips with a message naming the
missing variable when its key is blank (and the Ollama test skips when Ollama isn't
reachable). They cover: Gemini generate/stream/embed and a Gemini node in a graph; Groq and
OpenRouter generation; Ollama generation; a fallback chain reporting `provider_used`; SMTP +
IMAP login and an inbox read; the `/api/integrations/{provider}/test` endpoint; and the full
**Input → Gemini → Gmail → Output** run through the API, which sends a real email from
`SMTP_USER` to `SMTP_USER` and then confirms it arrived by reading the inbox with a Gmail Read
workflow. Keys are never printed. Free tiers have small daily quotas, so don't run the live
suite in a loop.

**Async execution and WebSocket tests** use no Celery eager mode and no mocks of Redis:

- `test_async_runs.py` and `test_websocket.py` call the worker's own code
  (`app.services.runs.run_execution`) in-process, as a background task, against the test
  database and a **real Redis** (database 15, so the dev worker's queue on db 0 is never
  touched). The socket is driven in-process with `httpx-ws`. They cover 202 + enqueue, the
  pending → running → success/failed/stopped transitions and their event order (checked
  against the database at each event), stop semantics, stale-heartbeat and restart
  recovery, idempotent re-delivery, time limits, token events, WS auth/ownership, snapshot
  replay, heartbeats, and Redis-drop resync. Only the task *sender* is faked in the API
  route tests (to assert what would be queued).
- `test_worker_integration.py` starts a **real Celery worker** in a thread
  (`celery.contrib.testing.worker.start_worker`, solo pool) on the real Redis broker, with
  committed rows, and checks a queued run, a double enqueue running once, and a stop through
  the worker.

**Phase 3.5 tests:**

- `packages/workflow-engine/tests/test_documents.py`: PDF Extract and OCR on real files made
  in the test (a text PDF, an image-only "scan", PNGs) with real PyMuPDF and Tesseract,
  including page ranges, wrong types, missing files, and missing language data. Summarize
  and Entity Extraction run against scripted providers: prompts, truncation, the fallback
  chain, schema validation and normalization (`"4,389.20"` → `4389.2`), the one retry on
  invalid JSON, and failure after two bad replies. Also Input nodes of type File, and a whole
  document pipeline.
- `test_queue_handoff.py` (engine and API): every node type's queue, where runs start, a run
  paused at a node its worker doesn't serve and resumed elsewhere, skipped nodes never
  causing a hand-off, and, against the database, a run moving from a "worker-llm" to a
  "worker-default". That covers the rows and events on each side, each segment running
  once, stopping and recovering a run that's waiting between queues, and a hand-off that
  can't be queued.
- `test_files_api.py`: type sniffing (including a ZIP renamed `.pdf`), size limits (declared
  and streamed), `411`, empty files, name sanitizing and path tricks, owner-only access to
  metadata, content, and delete, and file inputs on runs (someone else's file is refused).
- `test_netguard.py`: the SSRF guard. Blocked and allowed addresses and names, and the
  node refusing internal URLs. With fake DNS answers, it refuses a public-looking name that
  resolves inside (DNS rebinding), and a mixed answer. A real local HTTP server shows that
  a redirect to an internal host is refused, that the socket goes to the checked address,
  and that `HTTP_ALLOW_PRIVATE_NETWORKS` turns the guard off.

**Deployment tests** (`test_deployments.py`):

- **Keys:** the key is returned once, the row holds its SHA-256 and prefix (the key itself
  appears in no column and no log line), listings never include it, and the response is
  `no-store`. A redeploy keeps the id and key. Rotating revokes the old key at once and
  leaves the deployed graph alone.
- **Snapshot:** runs use the deployed graph until a redeploy, and an invalid graph isn't
  deployed. Deleting the workflow removes the deployment.
- **Auth and ownership:** no key, a wrong key, a key off by one character, another
  deployment's key, a user's JWT, Basic auth, and an unknown deployment all get the same
  `401`. Other users can't deploy, rotate, list, or see the runs, and a key can't read
  another deployment's runs or the owner's manual runs.
- **Async and wait modes:** async answers `202` and queues on the right queue, and the
  status link follows the run to its final output. For `?wait=true`, a task queue starts
  the run in-process as soon as it's queued, so the request really waits: `200` with the
  output, `200` with `failed` and the error, `202` at the timeout, the timeout's upper
  bound, and the database-polling fallback when Redis events are unavailable.
- **Errors and limits:** bad inputs, someone else's file, a graph that no longer validates
  (`422`), a broker outage (`503`), and the per-deployment rate limits on runs and on status
  polls (another deployment keeps its own budget, and requests with a bad key don't count).

**Phase 5 tests:**

- `apps/api/tests/test_triggers.py`: schedule math (a time zone, fixed times firing once when
  clocks fall back, intervals keeping their spacing, a time skipped by spring forward, invalid
  crons and zones, a cron that never fires); a fire time starting exactly one run when two
  ticks see it; the unique event key as a second guard; **a real race**: four sessions on
  separate database connections claiming one committed fire time, with exactly one run;
  scheduled runs in the history with `trigger=schedule`; misfires skipped instead of run late;
  the hourly cap; three failures switching a trigger off (and turning it back on clearing
  that), a success resetting the count, and a run that can't start being recorded as failed.
  The email trigger against an in-memory mailbox with real UID semantics: enabling starts at
  the folder's end; a new email runs **exactly once** through repeated polls, a lost position,
  and the same message under a new UID; filters, oldest-first backlog with `max_per_poll`, a
  text input, a renumbered folder, an unreadable mailbox, **Check now**, and claiming each
  poll once on the minute. The webhook: runs recorded as `webhook`, `409` when off, `429` over
  the cap.
- `apps/api/tests/test_templates.py`: the catalog and its requirements, every template graph
  validating, **Use template** (an editable copy, triggers off, the time zone, unique names,
  the sample invoice as the default file), fitting providers (Groq-only, Discord-only), node
  state moving only with successful runs, the CSV/JSON download, and Telegram/Discord
  credentials (masked, validated, the user's bot replacing the server's).
- `packages/workflow-engine/tests/test_lists.py`: For Each concurrency never exceeding its
  limit; the rate limit on a fake clock (bursts at t = 0, 60, 120 s, never more than N starts
  in a 60 s window); partial failures per item and `fail_when`; JSON replies; `{{item}}`
  fields and variables; template mode, `flatten`, `max_items`; the time budget; Filter and
  Join; and `{{item}}` being valid only in per-item fields.
- `packages/workflow-engine/tests/test_feeds.py`: RSS 2.0 and Atom parsing, newest first,
  "since last run" (and a test run not moving it), non-feeds, `404`s, the SSRF guard for both
  nodes, and Web Page extraction, truncation, and refused binaries.
- `packages/workflow-engine/tests/test_notify.py`: the exact Telegram `sendMessage` and
  Discord payloads; Markdown to Telegram HTML (and escaping); the default chat; `429` waiting
  `retry_after` then succeeding (Telegram's `parameters.retry_after`, Discord's body and
  `Retry-After` header); giving up when asked to wait too long; the plain-text fallback when
  Telegram rejects the markup; splitting long messages with nothing lost; helpful errors
  (`401`, chat not found, bot blocked); the token and webhook never appearing in errors; the
  connection checks; and credential validation.
- `packages/workflow-engine/tests/test_documents.py` also covers OCR's `prefer_text_layer`.

**Fallback timing** (`packages/workflow-engine/tests/test_nodes.py`): a provider that would
answer long after its share of the node's timeout is given up on, and the fallback answers
(with and without streaming) in about half the timeout. Without fallbacks a provider keeps
the whole timeout, and in a chain of three the last provider gets all the time that's left.

Locally (venv, with the dockerized Postgres and Redis running): `cd apps/api && pytest`. The
document tests need Tesseract installed (they skip without it); the Docker image has it.

### Web unit tests (Vitest)

```bash
cd apps/web
npm install
npm test
```

They cover the editor store (add, delete, duplicate, connect, undo/redo and coalescing; the
save state machine: debounced revisions, one save in flight, re-saving edits made during a
save, ignoring stale responses, errors), `{{` reference suggestions and insertion, the JSON
Schema → form mapping and its Zod rules (checked against the backend's messages), graph
conversion and placement, node config summaries (whole values, cut only by the card), the
run-state reducer (snapshots, seq de-duplication, token streaming), the WebSocket client
(first-message auth, 4401 refresh-once, 4404, reconnect backoff), and the Deploy dialog's
helpers (Input/Output listing, example inputs, shell quoting, the curl command), and the
Triggers panel's helpers (trigger states, times in a schedule's own zone, `{{item}}`
suggestions, the new nodes' summaries).

### End-to-end tests (Playwright)

These drive a real Chromium against the running stack, with nothing mocked:

```bash
docker compose up -d
docker compose exec api python -m app.db.seed
cd apps/web
npx playwright install chromium     # first time only
npx playwright test                 # E2E_BASE_URL / E2E_API_URL override the defaults
```

- `e2e/editor.spec.ts` builds scratch pipelines through the API (deleted afterwards) and
  checks drag-and-drop from the library, connecting, autosave and reload,
  duplicate/rename/collapse/multi-select/delete with undo and redo, the schema form, `{{`
  autocomplete, variables, inline reference errors, Test node, a failed save with Retry, and a
  deliberately broken graph (a cycle and a missing required field) showing errors on its
  nodes.
- `e2e/run.spec.ts` opens the seeded pipeline, edits the prompt and turns on streaming
  (autosaved), runs it, and watches the nodes turn blue and then green over the WebSocket,
  asserting that the LLM's tokens (Gemini's, or Groq's as the fallback) reached the browser.
  It confirms the **real email** arrived by
  reading the inbox over IMAP with a Gmail Read node, then opens the execution detail page;
  the seeded graph is restored afterwards. It also stops a running workflow and joins one
  that's already in progress.
- `e2e/documents.spec.ts` opens the seeded document pipeline and checks that the library
  has the Documents group and that the node cards show their queues. It uploads the sample
  scan through the run form, runs it, and watches OCR go blue and then green before the run
  moves to the LLM workers. The run panel must show OCR on `worker-ocr@...` and Summarize and
  Entity Extraction on `worker-llm@...`, the OCR text as readable text, and the entities
  (people, the invoice number, the total) as lists. The uploaded copy is deleted afterwards.
- `e2e/deploy.spec.ts` opens the seeded pipeline, checks the Deploy dialog's name, inputs,
  and outputs, and deploys it (or, if an earlier run deployed it, redeploys and generates a
  new key, since the old one can't be shown again). It checks the curl command and its Copy
  button, then calls the endpoint from outside the browser: `401` without the key or with a
  wrong one, then `?wait=true` with the key, which must return `200` with Gemini's summary and
  the Gmail receipt. The status link answers with the same key, the run is in the owner's
  history with trigger `webhook`, and the reopened dialog shows only the key's prefix. The
  pipeline stays deployed, but the key the test used is revoked at the end.
- `e2e/triggers.spec.ts` checks the dashboard's templates and their requirements, **Use
  template** opening an editable copy with its schedule off, the Triggers panel saving a
  schedule in `Asia/Kolkata` (the server's preview and the stored next fire time both 07:30
  IST), switching it off, the email trigger's setup, and the executions list's trigger badges
  and filter.
- `e2e/audio.spec.ts` creates a Meeting Notes copy with **Use template**, uploads
  `samples/team-meeting.mp3` through the run form, and runs it for real: Speech to Text must run
  on `worker-audio@...`, the transcript and its timestamped segments must show, the validated
  notes must name Elena and Marcus, and the notes are posted to Telegram. The recorder tests
  use Chromium's fake microphone: **Start recording** stays disabled until the consent box is
  ticked, a recording of a few seconds is uploaded as `audio/webm`, and a page whose
  Permissions-Policy blocks the microphone shows "Microphone access was blocked". The copies
  and uploads are deleted afterwards.
- `e2e/pages.spec.ts` covers the dashboard, execution history filters and detail, the
  socket's 4401/4404 closes, and the integrations page: real tests of the server's Gemini and
  Gmail credentials, and connecting then disconnecting a throwaway key on a provider you
  haven't stored one for.

The run and deploy tests each call Gemini and send one email, so they need `GEMINI_API_KEY`,
`SMTP_USER`, and `SMTP_PASSWORD`. With `GROQ_API_KEY` too, the seeded pipelines fall back to
Groq when Gemini is overloaded, so they pass through Gemini outages. Before running a seeded
pipeline, the specs check that it validates, and fail at once with the backend's reasons if
it doesn't. Screenshots are written to [`docs/screenshots`](docs/screenshots). The dev server
runs with `devIndicators: false`, so the Next.js badge isn't in them.

### Phase 5 verification on real services

Run against the stack above (demo account; Gemini with Groq as fallback, the Gmail App
Password, a real Telegram bot), with nothing mocked:

| Check | Result |
| ----- | ------ |
| A schedule fires on its own | An every-minute schedule on a Text → Output pipeline fired at 18:50:00, 18:51:00, ... 18:56:00: 7 runs, one per minute, all `success`, each with `trigger: schedule`, its `trigger_id`, and no user, in the executions list with the schedule filter |
| A real email triggers exactly one run | An email to the Gmail account with a unique subject, filtered on by an Email Triage trigger: the next poll found it and started **one** run (event key = its Message-ID); **Check now** and three later polls found nothing new, with one `trigger_events` row and one execution. Gemini classified it `urgent` and the Telegram alert was delivered. (Gmail marks mail sent to yourself as read, so the test trigger had Unread only off; the first test email, sent while it was on, was correctly never run) |
| Telegram | **Test connection** in Integrations: `getMe` named the bot and `getChat` the private chat; every template's Telegram step delivered a real message (`mock: false`) |
| Morning Digest from the inbox and a real feed | Created with **Use template** and fired by its schedule: 10 unread emails + 8 BBC News stories → For Each (18 items, 0 failed, 53 s of rate-limit waits) → a Gemini digest with Inbox and News sections → delivered to Telegram, run `success`, `trigger: schedule` |
| The other templates | Invoice Extractor on the sample scan: OCR on `worker-ocr`, entities on `worker-llm` (vendor, customer, invoice number HF-2026-0417, total due EUR 4,389.20, due date), CSV download. Job Alert Filter on the live We Work Remotely feed: 15 postings scored, 2 at ≥ 70 sent to Telegram |

### Phase 6 verification on real services

Same stack and account, nothing mocked. The sample recording,
[`samples/team-meeting.mp3`](samples/team-meeting.mp3) (100 s, 602 KB), is a made-up planning
meeting written for this repository and spoken by the Windows text-to-speech voices (Zira,
David, Hazel) with [`samples/make_meeting_sample.ps1`](samples/make_meeting_sample.ps1), so it
can be used freely.

| Check | Result |
| ----- | ------ |
| Groq Whisper on the sample | 1 chunk, 1.5 s on `worker-audio`, language `en`, 10 segments from 00:00 to 01:38 with the speakers' pauses between them, plus the hallucinated "Thank you" below. The vocabulary prompt fixed "Groq" and "docs" (heard as "Grok" and "docks" without it) |
| Chunking and stitching on real audio | The same file through the node with `chunk_minutes` 0.75 (3 chunks, 1.8 s) and 0.5 (4 chunks, 2.0 s): each cut fell in a pause between sentences, and the segments ran on continuously from 00:00 to 01:38 across the cuts, matching the one-chunk timings to within a second. The engine tests do the same on a generated tone with known silences (cuts at 18.5 s and 38.5 s) |
| A Whisper hallucination | Groq returned a trailing "Thank you" (another time "Terima kasih", 100.08-130.06 s on a 100.3 s file, `avg_logprob` -1.07): now dropped by Whisper's confidence rule (see [Speech to Text](#audio-speech-to-text)) |
| Local faster-whisper | `base` on the worker's CPU: 6.9 s for the 100 s file (model downloaded into the `whisper_models` volume on first use), language `en` (p = 0.999) |
| Meeting Notes end to end | **Use template** → run: Speech to Text on `worker-audio`, Structured Output on `worker-llm` (Gemini, valid on the first attempt: 2 decisions, 3 action items with owners Elena and Marcus and due dates Friday, Monday, today), Telegram on `worker-default`: delivered to the chat (`mock: false`), run `success` in 6.2 s |
| Web Research end to end | "What changed between HTTP/2 and HTTP/3, and why does HTTP/3 run over UDP?": DuckDuckGo returned 5 results (dev.to, bigiron.cc, speedtesthq.com, statuscodefyi.com, panelica.com), the top page was read (trafilatura), and Gemini answered in two paragraphs citing [1]-[3] with a `Sources:` list of titles and URLs; run `success` in 6.1 s |
| New LLM providers | Custom (OpenAI-compatible) pointed at Groq's endpoint (`https://api.groq.com/openai/v1`, `openai/gpt-oss-20b`): a real answer in 880 ms, `provider_used: custom`. A Custom base URL of `http://169.254.169.254/v1` was refused ("Blocked"). Mistral, Cerebras, and Tavily had no key here, so their live tests skip with "MISTRAL_API_KEY is blank; set it in .env to run this live test" |
| E2E | `e2e/audio.spec.ts`: 3 passed (Meeting Notes from an upload, the recorder with the consent gate, a blocked microphone) |

Re-run the provider checks with `docker compose exec api pytest -m live -s
/packages/workflow-engine/tests/test_live_providers.py`; each test without its key skips and
names the variable.

## Database migrations

Migrations live in [`apps/api/app/alembic/versions`](apps/api/app/alembic/versions). The API
container applies them automatically on startup. To run Alembic yourself:

```bash
docker compose exec api alembic upgrade head        # apply all migrations
docker compose exec api alembic current             # show the current revision
docker compose exec api alembic downgrade -1        # roll back one revision
docker compose exec api alembic revision --autogenerate -m "describe change"   # after editing models
docker compose exec api alembic check               # fail if models and migrations have drifted
```

Review every autogenerated migration before committing. Autogenerate doesn't drop Postgres
ENUM types in `downgrade()`, for example (see the initial migration for the pattern).

To inspect the schema:

```bash
docker compose exec postgres psql -U flowforge -d flowforge -c "\dt"
```

## Seed data

[`apps/api/app/db/seed.py`](apps/api/app/db/seed.py) creates the demo user
`demo@flowforge.ai` / `demo1234` and a ready pipeline, **Demo: Summarize and email**: Input
(`topic`) → Gemini (streams its answer) → Gmail (to `{{vars.recipient}}`, set to `SMTP_USER`)
→ Output. It uses the real providers, so it validates and runs from the UI once the keys are
in `.env`. With `GROQ_API_KEY` set, its Gemini node falls back to Groq, as the document
pipeline's LLM nodes do: Gemini's free tier answers 429 or "high demand" 503 at times. A
demo pipeline seeded before that has no fallback; add `groq` to the Gemini node's
**Fallback** in the editor. It also stores `samples/scanned-invoice.pdf` as one of the demo user's uploads and
creates **Demo: Scanned invoice to entities** (Input(File) → OCR → Summarize → Entity
Extraction → Output; see [Document AI](#document-ai)), whose file input defaults to it. The
seed is idempotent (pipelines are matched by owner and name, the sample by its checksum), so
it's safe to run more than once and never overwrites a pipeline you've edited.

```bash
docker compose exec api python -m app.db.seed          # while the stack is running
docker compose run --rm api python -m app.db.seed      # as a one-off container
```

## Running services outside Docker (optional)

Keep Postgres and Redis in Docker and run the API and/or web app on your machine:

```bash
docker compose up -d postgres redis
```

**API**, using a standard venv (no poetry/uv). This reads the repo-root `.env`, whose defaults
already point at `localhost:5433`. `requirements.txt` installs the workflow engine editable
from `../../packages/workflow-engine`, so run pip from `apps/api`:

```bash
cd apps/api
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt   # runtime deps + the engine + pytest
alembic upgrade head
python -m app.db.seed
uvicorn app.main:app --reload --port 8000
# and a worker for every queue (add --pool=threads on Windows):
celery -A app.worker.celery_app:celery_app worker -Q default,llm,ocr
# and, for triggers, exactly one scheduler:
celery -A app.worker.celery_app:celery_app beat
```

OCR needs the `tesseract` binary on your PATH (for example `apt install tesseract-ocr`,
`brew install tesseract`, or the UB Mannheim installer on Windows). Uploads go to
`apps/api/.data/files` unless you set `FILES_DIR`.

**Web:**

```bash
cd apps/web
npm install
npm run dev                     # http://localhost:3000
```

Stop the matching Docker service first (`docker compose stop api web`) so the ports are free.

## Providers and free API keys

Every LLM node has a `provider` (its type's default, e.g. a `groq` node uses `groq`), an
optional `model` (blank = the provider's default below), `system_prompt`, `user_prompt`,
`temperature`, `max_tokens`, and an optional `fallback` chain.

| Provider     | Node type    | Adapter                  | Default model (`*_MODEL` to change)  | Key                      |
| ------------ | ------------ | ------------------------ | ------------------------------------ | ------------------------ |
| Gemini       | `gemini`     | google-genai (AI Studio) | `gemini-3.5-flash-lite`              | `GEMINI_API_KEY` (free)  |
| Groq         | `groq`       | OpenAI-compatible        | `openai/gpt-oss-20b`                 | `GROQ_API_KEY` (free)    |
| OpenRouter   | `openrouter` | OpenAI-compatible        | `openrouter/free` (any `…:free` model works) | `OPENROUTER_API_KEY` (free) |
| Mistral      | `mistral`    | OpenAI-compatible        | `ministral-8b-latest`                | `MISTRAL_API_KEY` (free plan) |
| Cerebras     | `cerebras`   | OpenAI-compatible        | `qwen-3.8-27b` (or `gpt-oss-120b`)   | `CEREBRAS_API_KEY` (free) |
| Custom       | `custom_llm` | OpenAI-compatible        | `CUSTOM_OPENAI_MODEL` (required)     | `CUSTOM_OPENAI_BASE_URL`, optional `CUSTOM_OPENAI_API_KEY` |
| Ollama       | `ollama`     | OpenAI-compatible        | `llama3.2`                           | none (local)             |
| OpenAI       | `openai`     | OpenAI-compatible        | `gpt-4.1-mini`                       | `OPENAI_API_KEY` (paid)  |
| Anthropic    | `anthropic`  | anthropic SDK            | `claude-opus-5`                      | `ANTHROPIC_API_KEY` (paid) |

Keys in `.env` are server-wide defaults. Each user can also store their own on the
**Integrations** page (`/integrations`), which takes priority for their runs; **Test
connection** there checks a key with a real, minimal call.

**How to get each free key:**

- **Google AI Studio (Gemini):** sign in at <https://aistudio.google.com/apikey>, click
  **Create API key**, and put it in `GEMINI_API_KEY`. Free-tier quotas are per model and per
  day and they change: when this was written, `gemini-3.5-flash-lite` allowed about 500 free
  requests/day and `gemini-3.8-flash` only 20, hence the default. Check yours at
  <https://aistudio.google.com/rate-limit>.
- **Groq:** create an account at <https://console.groq.com>, open **API Keys**
  (<https://console.groq.com/keys>), create one, and set `GROQ_API_KEY`. Current models:
  <https://console.groq.com/docs/models>.
- **OpenRouter:** sign up at <https://openrouter.ai>, create a key at
  <https://openrouter.ai/settings/keys>, and set `OPENROUTER_API_KEY`. Free models end in
  `:free` (for example `google/gemma-4-31b-it:free`). The default `openrouter/free` picks one of
  whichever free models are available. The list: <https://openrouter.ai/models?max_price=0>.
- **Mistral:** sign in at <https://console.mistral.ai>, choose the free **Experiment** plan
  (it asks to verify a phone number), create a key at <https://console.mistral.ai/api-keys>, and
  set `MISTRAL_API_KEY`. Base URL `https://api.mistral.ai/v1`; models:
  <https://docs.mistral.ai/getting-started/models/>.
- **Cerebras:** sign up at <https://cloud.cerebras.ai>, create a key, and set
  `CEREBRAS_API_KEY`. Base URL `https://api.cerebras.ai/v1`; the free tier's models were
  `gpt-oss-120b` and `qwen-3.8-27b` when checked (<https://inference-docs.cerebras.ai/models/overview>).
- **Custom (OpenAI-compatible):** any server with `/v1/chat/completions`, e.g. Together,
  Fireworks, DeepInfra, a vLLM or LM Studio server. Set `CUSTOM_OPENAI_BASE_URL` (e.g.
  `https://api.together.xyz/v1`) and `CUSTOM_OPENAI_MODEL`, plus `CUSTOM_OPENAI_API_KEY` if
  it needs one, or connect it under Integrations (base URL and model required, key optional).
  The base URL goes through the [SSRF guard](#security-outbound-requests-ssrf-guard): it must
  resolve to a public address, and so must every request, so it can't be pointed at
  `localhost`, a private network, Docker services, or cloud metadata. For a server on your
  own machine use Ollama's provider, or `HTTP_ALLOW_PRIVATE_NETWORKS=true` in development.
- **GitHub Models:** not offered. GitHub retired it on 2026-07-30
  (<https://github.blog/changelog/2026-07-30-github-models-is-now-retired/>); use Custom for
  another OpenAI-compatible endpoint.
- **Ollama (no key):** install it from <https://ollama.com/download>, then pull a model and
  make sure the server is running:

  ```bash
  ollama pull llama3.2           # the default OLLAMA_MODEL
  ollama pull nomic-embed-text   # optional, for embeddings
  ollama serve                   # if it isn't already running as a service
  ```

  From the API container Ollama is reached at `http://host.docker.internal:11434/v1` (the
  default `OLLAMA_BASE_URL` in `.env.example`; Compose maps the name with `extra_hosts`, so it
  works on Linux as well). For a venv run of the API, set `OLLAMA_BASE_URL=http://localhost:11434/v1`.

**Credentials and the no-mock policy.** A run uses your own stored credential for a provider
if you've connected one (`/api/integrations`, below), otherwise the server-wide key from `.env`.
If neither exists, `/validate` reports an `auth_missing` error, and `/run` returns `422` with
that message without running anything:

```json
{"code": "auth_missing", "node_id": "groq", "field": "provider",
 "message": "Node 'groq': Authentication missing for provider 'groq': no API key configured. Set GROQ_API_KEY on the server, or connect a groq credential under Integrations"}
```

Mocks are used only by the test suite (`TESTING=true`) or when a node explicitly sets
`"provider": "mock"` (email nodes: `"auth": "mock"`).

**Fallback chains.** `"fallback": ["groq", "openrouter:google/gemma-4-31b-it:free", "ollama"]`
tries each provider (optionally `provider:model`) in order when the one before it fails. The
node output reports `provider` (configured), `provider_used` (who answered), `model`, `mock`,
and `fallback_errors` (what failed on the way), and the API logs which provider answered.
Providers in the chain are credential-checked at validation time too. The node's timeout
(`WORKFLOW_NODE_TIMEOUT_SECONDS`) is shared out along the chain: each provider with fallbacks
after it gets an equal share of the time left, and the last one gets all of it. Without that,
a provider that is slow to fail would use up the whole timeout. An overloaded Gemini can take
a minute per attempt to answer 503 or 504, and those are retried. In that case it's given up
with `gemini: no answer within 60s, its share of the node's time` in `fallback_errors`. A
node without fallbacks still gets the whole timeout.

**Rate limits and errors.** HTTP 429, 408, 5xx, and network errors are retried with
exponential backoff and jitter (`LLM_MAX_RETRIES`, `LLM_RETRY_BASE_DELAY_SECONDS`,
`LLM_RETRY_MAX_DELAY_SECONDS`). A provider's `Retry-After` / Gemini `RetryInfo` is honoured.
If it asks for longer than the max delay, the call fails right away rather than stalling the
workflow, so a fallback provider can answer. Nothing hardcodes free-tier limits. The final
error lands in the node result, for example `gemini: rate limited (HTTP 429): You exceeded your
current quota ... [quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier=20]
(gave up after 1 attempt; provider asked to wait 48s, longer than the 30s retry limit)`.
Auth errors (401/403) and bad requests are not retried.

**Streaming and embeddings.** Every adapter implements `generate()` and `stream()` (token
deltas). `embed()` works for Gemini (`gemini-embedding-2`), Ollama (`nomic-embed-text`), and
OpenAI; Groq, OpenRouter, and Anthropic raise a clear `ProviderNotSupportedError`. Streaming
is available at the provider level; it isn't pushed to clients yet (that arrives with
WebSockets).

## Gmail: create an App Password

The Gmail nodes log in to `smtp.gmail.com` (send) and `imap.gmail.com` (read) with your Gmail
address and a **Google App Password**. Your normal Google password won't work: Gmail answers
`534 5.7.9 Application-specific password required`.

1. Turn on **2-Step Verification**: <https://myaccount.google.com/signinoptions/two-step-verification>.
   App Passwords only exist for accounts with it enabled.
2. Open **App passwords**: <https://myaccount.google.com/apppasswords>. If the page says the
   setting isn't available, 2-Step Verification is off, or your Workspace admin has disabled
   App Passwords.
3. Enter a name (e.g. "FlowForge") and click **Create**. Google shows a 16-letter password like
   `abcd efgh ijkl mnop`. Copy it; it's shown once. The spaces are optional (FlowForge
   strips them).
4. Put your address in `SMTP_USER` and the App Password in `SMTP_PASSWORD` (server-wide), or
   connect it per user with `POST /api/integrations/gmail/connect`
   `{"email": "you@gmail.com", "app_password": "abcd efgh ijkl mnop"}`.
5. Check it with `POST /api/integrations/gmail/test`, which logs in to SMTP and IMAP without
   sending anything.

IMAP is always on for personal Gmail accounts. Revoking the App Password at the same page
cuts FlowForge off immediately. SMTP defaults to port 587 with STARTTLS (`SMTP_PORT=465` uses
implicit TLS), and the SMTP/IMAP calls run in a worker thread so they never block the server.
Other providers work too: set `SMTP_HOST`/`SMTP_PORT`/`IMAP_HOST`/`IMAP_PORT`.

**Gmail node** (`gmail`): `auth` (`gmail` = your stored credential, else the server's; `mock`
= send nothing), `to`, `cc`, `bcc` (lists or comma-separated), `subject`, `body` (plain text),
optional `html_body`, and `attachments`: `[{filename, content, encoding: "text"|"base64",
content_type}]`, usually filled from references like `{{http.body}}`. Output: `message_id`,
`status`, `from`, `to`, `cc`, `bcc`, `subject`, `attachments`, `sent_at`, `mock`.

**Gmail Read node** (`gmail_read`): `auth`, `folder` (default `INBOX`), `from_address`,
`subject`, `unread_only` (default true), `since_days`, `max_results` (default 10, max 50),
`mark_as_read` (default false: messages are read with `BODY.PEEK`), `include_body`,
`max_body_chars`. Output: `{{gmail_read.emails}}`, a newest-first list of `{uid, message_id,
from, from_address, to, cc, subject, date, unread, snippet, body_text, body_truncated,
attachments: [{filename, content_type, size}], size}`, plus `count` and `folder`.

## Asynchronous execution

```
                    POST /api/workflows/{id}/run  ->  202 {execution_id}
  +--------+  ------------------------------------------------>  +------------------+
  | client |                                                     |  api (FastAPI)   |
  |        |  <------------------------------------------------  |  REST + WS       |
  +--------+    WS /ws/executions/{id}: snapshot + live events   +---+-----------+--+
      |                                                              |           |
      |  GET /api/executions/{id}             reads state (snapshot) |           | send_task(queue)
      |                                                              v           v    SUBSCRIBE
      |                                                   +------------+   +--------------------+
      +-------------------------------------------------> | PostgreSQL |   | Redis              |
                                                          |            |   |  broker (queues)   |
                                                          +------------+   |  pub/sub (events)  |
                                                                ^          |  stop flags, seq   |
                                           node + run rows,     |          +--+--------------+--+
                                           heartbeat            |     consume |              ^ PUBLISH events,
                                                                |             v              | poll stop flag
                                                          +-----+------------------------------+
                                                          | worker(s): Celery, prefork pool    |
                                                          |   run_execution -> flowforge_engine|
                                                          +------------------------------------+
```

**Lifecycle.** `POST /run` validates the graph (including `auth_missing`), records a
`pending` execution with one `pending` row per node, plus the run's inputs and a snapshot of
the graph (later edits don't affect a queued run), and sends a Celery task named
`flowforge.run_execution` whose task id *is* the execution id. A worker then:

1. **claims** it with a compare-and-set (`UPDATE ... SET status='running' WHERE status='pending'`).
   A second delivery of the same task, or a run that was stopped while queued, fails the
   claim and does nothing, so each execution runs **at most once**;
2. runs the graph with the engine, writing each node's transition (`pending → running →
   success | failed | skipped`, with input, output, error, timing) and publishing an event
   after each commit;
3. heartbeats `heartbeat_at` every `EXECUTION_HEARTBEAT_SECONDS` and polls a Redis stop flag
   every `EXECUTION_STOP_POLL_SECONDS`;
4. finishes the execution as `success`, `failed`, or `stopped` (again compare-and-set, so it
   never overwrites a stop or a recovery that happened meanwhile), **or hands it off** when
   the next node belongs to a queue it doesn't consume (below).

**Queue hand-off.** Every node type declares a queue: `llm` for model calls (the LLM nodes,
Summarize, Entity Extraction), `ocr` for document processing (OCR, PDF Extract), and
`default` for the rest (HTTP, Gmail, Delay). Input, Output, Text, and Condition are
*portable*: they run wherever the run is. A run is queued on the queue of its first
non-portable node. Its worker runs nodes as long as they're portable or on a queue it
consumes. At the first node it can't run, it records the hand-off and sends a continuation
task, `flowforge.run_execution(execution_id, segment)`, to that node's queue. The hand-off is
a compare-and-set on its own ownership that sets `segment += 1`, no worker, no heartbeat,
`handoff_at = now`, and `queue = next`. The next worker claims that segment with another
compare-and-set (`WHERE segment = N AND worker_hostname IS NULL`), so each segment runs once
however often its task is delivered. It rebuilds the finished nodes' results from their
rows, then carries on. The execution stays `running` throughout. Each node row records the
`queue` and `worker_hostname` that ran it, and the events `execution.handoff` and
`execution.resumed` mark the move. A worker that consumes every queue never hands off.

`?sync=true` runs the same code inside the request (and still publishes events) and returns
`200` with the finished execution, which is handy in Swagger and scripts.

**Reliability rules.**

- *Late acks.* `task_acks_late` with `worker_prefetch_multiplier=1`: a worker that dies before
  or during a run doesn't lose the message. Redelivery is harmless thanks to the claim.
- *Retries only for infrastructure.* If the database is unreachable *before* the claim, the
  task retries with backoff (`CELERY_TASK_MAX_RETRIES`). After the claim nothing is retried:
  node failures (API errors, bad input) are the run's result, and re-running could send an
  email twice.
- *Time limit.* `EXECUTION_TIME_LIMIT_SECONDS` stops a run cooperatively and marks it
  `failed` ("Execution exceeded the time limit of 600s"). Celery's soft/hard limits sit 30/60 s
  above it as a backstop, and Redis' visibility timeout is longer still, so a long run isn't
  redelivered while it's running.
- *Crash recovery.* A worker records its node name (`worker@<container hostname>`). On
  startup it immediately fails executions its previous incarnation left `running` (the
  container restarted). Independently, the API sweeps every
  `EXECUTION_RECOVERY_INTERVAL_SECONDS` for `running` executions whose heartbeat is older than
  `EXECUTION_STALE_AFTER_SECONDS` (the worker was killed and didn't come back) and for
  `pending` ones older than `EXECUTION_PENDING_TIMEOUT_SECONDS`. Recovered runs get a clear
  `error_message` (e.g. *"No heartbeat from worker 'worker@0df0151afe8e' for 41s: it crashed or
  was killed while running this execution"*), their running node is `failed`, and the rest
  are `skipped`; the matching events are published so watchers finish too. If a worker that
  is actually alive finds its execution was finalized elsewhere, its next heartbeat notices
  and it abandons the run.
- *Stop.* `POST /api/executions/{id}/stop` marks a `pending` run `stopped` at once and revokes
  its task. For a `running` one it sets the stop flag. The worker cancels the current node if
  its type is `interruptible` (Delay, LLM calls, HTTP), or lets it finish (Gmail sending: never
  cut off mid-send), skips the remaining nodes, and marks the run `stopped`. The endpoint
  waits up to `EXECUTION_STOP_WAIT_SECONDS` and returns `200` with the final state (usually well
  under a second), `202` if the worker hasn't confirmed yet, or finalizes the run itself if
  the worker's heartbeat is stale. Only the owner can stop a run (others get `404`); a
  finished run gives `409`.
- *Between queues.* A run waiting for the next queue's worker has no heartbeat, so crash
  detection leaves it alone. Stopping it finishes it at once ("Stopped by user while waiting
  for a 'default' worker"), and a late continuation then finds nothing to claim. One that no
  worker takes within `EXECUTION_PENDING_TIMEOUT_SECONDS` is failed ("No worker for the
  'ocr' queue picked this run up ..."). The time limit counts the whole run, waits included.
  If the continuation can't be sent (the broker is down), the run fails with that reason.
- *Queues for new node types.* A heavy node type (GPU, say) sets `queue = "gpu"` and gets its
  own workers (`celery ... worker -Q gpu`) without other changes.

### Real-time events (WebSocket)

`WS /ws/executions/{execution_id}` streams one execution.

**Authentication** uses a JWT access token, either way:

- **first message** (recommended; keeps the token out of URLs and proxy logs): connect, then
  send `{"type": "auth", "token": "<access token>"}` within `WS_AUTH_TIMEOUT_SECONDS` (10 s);
- **query parameter**: `ws://localhost:8000/ws/executions/<id>?token=<access token>`.

| Close code | Meaning                                                            |
| ---------- | ------------------------------------------------------------------ |
| `1000`     | Normal: the execution finished and every event was delivered       |
| `4401`     | Not authenticated: missing, invalid, or expired token, or a first message that isn't `auth` |
| `4404`     | The execution doesn't exist or isn't yours (same rule as the REST API's `404`) |
| `1011`     | Internal error                                                     |

Every rejection is preceded by `{"type": "error", "code": 4401, "message": "..."}`.

**Message sequence.**

1. `{"type": "snapshot", "seq": N, "resync": false, "execution": {...}}`: the full current
   state from the database, the same shape as `GET /api/executions/{id}`. A client that
   joins late, or after the run finished, still gets the whole picture.
2. Live events with `seq > N`, in order. The server subscribes to Redis *before* reading
   the snapshot and drops events the snapshot already contains, so there are no gaps or
   duplicates.
3. After `execution.finished` the server closes with `1000`. Joining a finished execution
   yields the snapshot, `execution.finished` with `"replayed": true`, and the close.

Also: the server sends `{"type": "heartbeat"}` every `WS_HEARTBEAT_SECONDS` and answers
`{"type": "ping"}` with `{"type": "pong"}`. If Redis drops, it reconnects with backoff and sends
a new snapshot with `"resync": true`. As a safety net against a missed event, it also re-checks
the database every `WS_DB_CHECK_SECONDS`.

**Event schema.** Every event has `seq` (per execution, gapless, increasing across all
publishers), `type`, `execution_id`, and `timestamp` (ISO 8601, UTC). They're published on
Redis channel `flowforge:execution:<id>:events`, so other consumers can subscribe too.

| `type`               | Extra fields                                                                  |
| -------------------- | ----------------------------------------------------------------------------- |
| `execution.started`  | `status: "running"`, `workflow_id`, `worker`, `queue`, `started_at`, `nodes: [{node_key, node_type}]` |
| `node.started`       | `node_key`, `node_type`, `label`, `status: "running"`, `started_at`, `worker`, `queue` |
| `node.token`         | `node_key`, `text` (a streamed delta), `provider`; only for LLM nodes with `"stream": true` |
| `node.succeeded`     | `node_key`, `node_type`, `label`, `status`, `input` (resolved config), `output`, `started_at`, `finished_at`, `duration_ms` |
| `node.failed`        | same as `node.succeeded`, plus `error`                                          |
| `node.skipped`       | `node_key`, `node_type`, `label`, `status`, `reason`, timing when it was interrupted mid-run |
| `execution.handoff`  | `from_queue`, `to_queue`, `segment`, `worker` (the one letting go): the run waits for a `to_queue` worker |
| `execution.resumed`  | `segment`, `worker`, `queue`: the next worker picked it up                    |
| `execution.finished` | `status` (`success` / `failed` / `stopped`), `final_output`, `error`, `started_at`, `finished_at`, `duration_ms` |

```json
{"seq": 5, "type": "node.succeeded", "execution_id": "b2778fcf-...", "timestamp": "2026-09-28T08:05:22.188Z",
 "node_key": "gemini", "node_type": "gemini", "label": "Summarize", "status": "success",
 "input": {"user_prompt": "Write a three-sentence summary of event-driven architecture.", "...": "..."},
 "output": {"response": "Event-driven architecture (EDA) is ...", "provider": "gemini", "provider_used": "gemini",
            "model": "gemini-3.5-flash-lite", "mock": false, "fallback_errors": []},
 "started_at": "2026-09-28T08:05:20.433Z", "finished_at": "2026-09-28T08:05:22.188Z", "duration_ms": 1755}
```

**LLM token streaming.** Set `"stream": true` on an LLM node: the provider's streaming API
is used and each text delta is forwarded as `node.token`. The node's output, and every other
event, is the same as without streaming. If the fallback chain moves to another provider
mid-stream, later tokens carry that provider's name.

### Workers: queues and scaling

Compose runs one worker service per queue, from the API image (the models, credentials, and
engine are shared), with the prefork pool (one run segment per process at a time) and a
healthcheck based on `celery inspect ping`:

| Service          | Consumes  | Runs                                      | Processes per container (env)    |
| ---------------- | --------- | ----------------------------------------- | -------------------------------- |
| `worker-default` | `default` | HTTP Request, Gmail, Gmail Read, Delay    | `WORKER_DEFAULT_CONCURRENCY` = 4 |
| `worker-llm`     | `llm`     | LLM nodes, Summarize, Entity Extraction   | `WORKER_LLM_CONCURRENCY` = 8 (I/O-bound) |
| `worker-ocr`     | `ocr`     | OCR, PDF Extract                          | `WORKER_OCR_CONCURRENCY` = 2 (CPU-bound; `OMP_THREAD_LIMIT=1`) |
| `worker-audio`   | `audio`   | Speech to Text (ffmpeg + Whisper)         | `WORKER_AUDIO_CONCURRENCY` = 2 (`OMP_NUM_THREADS` = `AUDIO_THREADS_PER_TASK`, 2) |

```bash
docker compose up -d --scale worker-ocr=3          # three OCR containers, 6 OCR processes
docker compose up -d --scale worker-llm=2          # more LLM capacity, independently
docker compose logs -f worker-ocr                  # structured JSON logs, one line per event
docker compose exec worker-ocr celery -A app.worker.celery_app:celery_app inspect active   # what's running
```

Each container's node name is `<service>@<container id>`, unique per replica. It stays stable
across restarts, so a restarted worker fails the runs it died with. A service scaled to 0
simply leaves its queue's runs waiting; they fail after `EXECUTION_PENDING_TIMEOUT_SECONDS`.

**Seen working.** The document demo above ran with one container of each. From the
workers' own logs for that execution (filtered by its id; the timestamps are UTC):

```
worker-default: 0 log lines for this execution
worker-ocr:     13:16:03.872 task flowforge.run_execution[2f409b19-...] received
                13:16:04.281 node finished  input     success   21 ms
                13:16:05.875 node finished  ocr       success 1537 ms
                13:16:05.954 run handed off -> llm (segment 1)
worker-llm:     13:16:05.955 task flowforge.run_execution[2f409b19-...:1] received
                13:16:18.410 node finished  summarize success 12308 ms
                13:17:31.605 node finished  entities  success 73110 ms
                13:17:31.724 node finished  output    success    0 ms
                13:17:31.808 workflow run finished: success (segment 1)
```

Only `worker-ocr` touched the OCR node, and only `worker-llm` the LLM nodes, of the same run,
with 1 ms between the hand-off and the pickup. The same split is asserted by the document E2E
test and shows in each node row's `queue` and `worker_hostname`.

- **Shutdown:** `docker compose stop worker-ocr` sends SIGTERM. Celery stops taking tasks
  and waits up to `stop_grace_period` (30 s) for running ones; anything still running is
  killed and marked failed when the worker comes back (or by the API's sweep).
- **Outside Docker:** from `apps/api` with the venv active and Redis/Postgres up, run
  `celery -A app.worker.celery_app:celery_app worker -Q default,llm,ocr,audio` (and install
  ffmpeg). One worker on every
  queue never hands off. On Windows add `--pool=threads`, since prefork needs fork.

### Watching a run from the command line

[`scripts/watch_run.py`](scripts/watch_run.py) logs in, creates a workflow from a JSON graph
(or uses an existing one), queues a run, connects to the WebSocket (first-message auth by
default, `--auth-mode query` for `?token=`), and prints each event as it arrives with the
time since the `POST`. The password comes from `FLOWFORGE_PASSWORD` or a prompt.

It needs `httpx` and `websockets`. Either run it with the API's venv, which has both
(`apps/api/venv/Scripts/python scripts/watch_run.py ...` on Windows,
`apps/api/venv/bin/python ...` elsewhere), or install them into the Python you use:

```bash
python -m pip install -r scripts/requirements.txt
export FLOWFORGE_PASSWORD='...'
python scripts/watch_run.py --email you@example.com --register \
    --graph scripts/examples/gemini_gmail.json --var recipient=you@gmail.com \
    --inputs '{"topic": "event-driven architecture"}' --show-final
python scripts/watch_run.py --email you@example.com --graph scripts/examples/delay.json --var seconds=30 --stop-after 2
python scripts/watch_run.py --email you@example.com --graph scripts/examples/delay.json --connect-delay 8   # late join
python scripts/watch_run.py --email you@example.com --graph scripts/examples/stream_tokens.json --show-tokens
python scripts/watch_run.py --email you@example.com --execution-id <id>   # watch an existing run
```

Sample output (Input → Gemini → Gmail → Output on the real stack):

```
+     101 ms  POST /run -> 202 in 101 ms: execution b2778fcf-... status=pending queue=default
+     161 ms  [seq   0] snapshot            status=pending | input=pending, gemini=pending, gmail=pending, output=pending
+     257 ms  [seq   1] execution.started   worker=worker@0df0151afe8e queue=default
+     372 ms  [seq   4] node.started        gemini (gemini)
+    2185 ms  [seq   5] node.succeeded      gemini in 1755 ms provider_used=gemini -> Event-driven architecture (EDA) is ...
+    2224 ms  [seq   6] node.started        gmail (gmail)
+    6191 ms  [seq   7] node.succeeded      gmail in 3892 ms -> <179058272296.10.7892257543988529204@gmail.com>
+    6337 ms  [seq  10] execution.finished  status=success duration_ms=6169 final_output={"result": {...}}
+    6338 ms  WS closed by server: code=1000 reason=''
```

## Load test

[`loadtest/`](loadtest) measures API latency while heavy runs are in flight, and how run
throughput changes as `worker-ocr` scales. See [loadtest/README.md](loadtest/README.md) for
the scripts. Everything below is measured, from the raw files in
[`loadtest/results/20260928T135011Z/`](loadtest/results/20260928T135011Z) (Locust CSVs,
`throughput.jsonl`, `conditions.txt`), produced by `bash loadtest/run_suite.sh`.

**Conditions.**

- **Machine:** one laptop (13th Gen Intel Core i5-13420H, 8 cores / 12 threads, 15.6 GB
  RAM, Windows 11 Home 10.0.26200, on AC power), running the whole stack in Docker Desktop
  29.7.2 (a WSL2 VM with 12 CPUs and 7.6 GiB).
- **Stack:** the dev Compose stack. The API is a single uvicorn process (`--reload`), with
  Postgres 16 and Redis 7 on the same machine. `worker-default` ×1 (4 processes),
  `worker-llm` ×1 (8), `worker-ocr` ×1 or ×3 (2 processes each, one Tesseract thread each).
- **Load generator:** Locust 2.46.6 in a container on the Compose network (straight to
  `http://api:8000`), sharing the machine too.
- **Traffic:** "API users" each wait 0.5–1.5 s between requests. 3 in 4 requests are
  `GET /api/workflows`; 1 in 4 is `POST /api/workflows/{id}/run` of a no-op pipeline (one
  short task on `worker-default`). "In flight" drivers each keep one heavy run queued or
  running at all times. An OCR run is Input(File) → OCR → Output on the two-page sample scan
  at 300 dpi (about 1.7 s of Tesseract when uncontended). An LLM run is one short prompt to
  Groq.
- **Duration:** 2 minutes per scenario (the LLM one 1 minute), 10 users/s ramp. The
  database already held 4,192 executions from earlier runs.

**API latency** (milliseconds; driver polling excluded):

| Scenario | Users (API + in flight) | worker-ocr | `GET /api/workflows` p50 / p95 / p99 (requests) | `POST .../run` p50 / p95 / p99 (requests) | Failures (requests + runs) | Throughput (req/s) |
| --- | --- | --- | --- | --- | --- | --- |
| A: low, OCR | 5 + 2 OCR | ×1 | 17 / 26 / 42 (431) | 16 / 26 / 170 (162) | 0 of 1,284 (0 %) | 10.7 |
| B: high, OCR | 50 + 12 OCR | ×1 | 180 / 530 / 650 (3,456) | 280 / 720 / 870 (1,212) | 0 of 6,898 (0 %) | 57.8 |
| B: high, OCR | 50 + 12 OCR | ×3 | 480 / 950 / 1,100 (2,821) | 620 / 1,200 / 1,400 (910; max 55,015) | 0 of 5,557 (0 %) | 46.7 |
| C: low, LLM | 5 + 2 LLM | ×1 | 42 / 54 / 100 (208) | 20 / 33 / 200 (74) | 0 of 597 (0 %) | 10.1 |

**Heavy runs during those scenarios**, queued to finished as the drivers saw them (a 0.5 s
poll, so ±0.5 s):

| Scenario | worker-ocr | Runs finished | p50 | p95 |
| --- | --- | --- | --- | --- |
| A: 2 OCR in flight | ×1 | 113 | 2.1 s | 2.6 s |
| B: 12 OCR in flight | ×1 | 80 (0.67/s) | 18 s | 19 s |
| B: 12 OCR in flight | ×3 | 159 (1.34/s) | 8.8 s | 9.8 s |
| C: 2 LLM in flight | ×1 | 43 | 1.6 s | 5.8 s |

**Run throughput vs. `worker-ocr` replicas** (`loadtest/throughput.py`: 36 OCR runs queued
at once, no other load; timings from the API's own timestamps):

| worker-ocr | OCR processes | Batch time | Runs / minute | Queue wait p50 / p95 | Run time p50 / p95 | Failed |
| --- | --- | --- | --- | --- | --- | --- |
| ×1 | 2 | 32.2 s | 67.2 | 15.2 s / 28.3 s | 1.71 s / 1.82 s | 0 of 36 |
| ×3 | 6 | 17.1 s | 126.7 (×1.89) | 7.0 s / 13.6 s | 2.65 s / 3.10 s | 0 of 36 |

**Reading the numbers.**

- **Scaling OCR works, but on one machine it's bounded by the cores.** Three
  `worker-ocr` containers (6 Tesseract processes instead of 2) nearly doubled OCR throughput
  (×1.89), both in isolation and under API load (0.67 → 1.34 runs/s). It wasn't ×3 because
  each run got slower (1.71 s → 2.65 s): six Tesseract processes share this laptop's 8 cores
  (4 performance + 4 efficiency) with Postgres, Redis, the API, and Locust. On separate
  machines, replicas add capacity instead of sharing it.
- **The API is the bottleneck under high load, and OCR competes with it for CPU.** At
  high concurrency the single dev uvicorn process serves about 50–58 req/s, with `GET` p95
  around 0.5 s (×1) and 0.95 s (×3). With more OCR running at once, API latency rose about
  2.7× at p50. That's CPU contention: an earlier exploratory round of the same scenarios
  (its raw files were lost to a Git Bash path mix-up, so it isn't reported above) included
  a control, B with ×1 again right after ×3. It came back to ×1's numbers (`GET` p50 120 ms,
  p95 520 ms), so a growing executions table isn't the cause. For production: several API
  processes (`uvicorn --workers N`, no `--reload`) on a machine separate from `worker-ocr`.
- **No failures**: 0 among 13,941 API requests and 395 heavy runs in the Locust scenarios, and 72
  runs in the throughput batches.
- **Tail outliers, not explained:** 6 `POST /run` requests over the two rounds took 15–55 s,
  all with three OCR containers running. One of them, 55,015 ms, is in the table as that
  scenario's max. A [rerun of that scenario](loadtest/results/20260928T140053Z-rerun-b-high-ocr3) (983 POSTs, max 1.65 s) didn't reproduce it. The
  API logged no errors, and a timing log added to the run endpoint since then ("slow run
  request", when validation, the insert, and the enqueue take over 1 s) didn't fire. Postgres
  was in the middle of a slow time-based checkpoint (135 s of writes) during one of the two
  episodes, so the laptop's disk is a suspect, but that's unproven.
- **LLM numbers are the provider's, not ours.** Groq's free tier allows 30 requests per
  minute for `openai/gpt-oss-20b`. Two runs in flight made 45 calls in that minute, so 28 of
  them got `429 Rate limit reached` and were retried with backoff. That's why LLM run time
  jumps from 1.6 s (p50) to 5.8 s (p95). Gemini was answering `503 high demand` the same
  afternoon. Because free-tier limits, not the system, decide these results, the scaling
  comparison uses OCR-only runs.

## Security: outbound requests (SSRF guard)

A workflow author controls the HTTP Request node's URL, so by default it may only reach
public addresses (`flowforge_engine/netguard.py`):

- **Blocked:** loopback (`127.0.0.0/8`, `::1`), private networks (`10/8`, `172.16/12`,
  `192.168/16`, `fc00::/7`), link-local (`169.254/16`, which includes cloud metadata at
  `169.254.169.254`, and `fe80::/10`), carrier-grade NAT, multicast, reserved and unspecified
  addresses, and the IPv4-mapped IPv6 forms of all of these. Internal-looking names are
  refused before DNS: `localhost`, `*.internal`, `*.local`, and single-label names such as
  the Docker service names `postgres`, `redis`, or `api`.
- **Where:** in the connection pool's network backend, for every connection. The host name
  is resolved, every address it resolves to must be public, and the socket is opened to the
  address that was checked (TLS still verifies the name). So names that resolve inside are
  caught, including DNS rebinding, since there's no second lookup to race, and so is every
  redirect hop. Environment proxies are ignored, since a proxy would make the connection
  for us.
- **The error** says what happened: *"Blocked request to 'db.example.com' (resolves to
  10.1.2.3): it is a private network address. Set HTTP_ALLOW_PRIVATE_NETWORKS=true to allow
  internal addresses (local development only)."*
- `HTTP_ALLOW_PRIVATE_NETWORKS=true` turns it off, for calling services on your own machine
  during development.

Still to do before a public deployment: the per-user `base_url` of Ollama and OpenAI
credentials isn't guarded this way (the server's own `OLLAMA_BASE_URL` is expected to be
internal).

## API

### Auth

| Method | Path                 | Auth   | Description                                                |
| ------ | -------------------- | ------ | ---------------------------------------------------------- |
| GET    | `/api/health`        | none   | Liveness and database connectivity                         |
| POST   | `/api/auth/register` | none   | `{email, password, full_name}` → `201` access + refresh JWT |
| POST   | `/api/auth/login`    | none   | `{email, password}` → access + refresh JWT                 |
| POST   | `/api/auth/refresh`  | none   | `{refresh_token}` → new access token + rotated refresh token |
| GET    | `/api/auth/me`       | Bearer | The current user                                           |

- **Validation:** email format, and email is normalized to lowercase. Password must be 8–72
  bytes (72 is bcrypt's limit, so longer passwords are rejected instead of silently truncated).
  Full name can't be blank.
- **Errors:** `409` for a duplicate email; `401` for bad credentials (same message whether or
  not the email exists); `401` for a missing, invalid, or expired token or a refresh token sent
  as an access token; `422` for validation errors; `429` when rate limited.
- **Rate limiting:** login, register, and refresh each allow `AUTH_RATE_LIMIT` requests per
  client IP (default `10/minute`). Storage is in-memory for now, so limits reset when the API
  restarts.
- **Protecting new routes:** depend on `CurrentUser` from `app/api/deps.py`:

  ```python
  from app.api.deps import CurrentUser

  @router.get("/workflows")
  async def list_workflows(user: CurrentUser): ...
  ```

- **Tokens:** access tokens carry `type: "access"` and refresh tokens `type: "refresh"`;
  `decode_token(token, expected_type=...)` enforces the type, so neither works in the other's
  place. `/auth/refresh` returns a fresh pair. Tokens are stateless, so an older refresh token
  stays valid until it expires (server-side revocation isn't implemented yet).
- **Frontend refresh:** the web API client refreshes an access token it knows has expired
  before sending a request, and on any `401` refreshes once and retries. Concurrent requests
  share one refresh call. The user is sent to `/login` only if the refresh itself is rejected
  or the refresh token has expired.
- **Logging:** every request produces one JSON log line (`request_id`, `method`, `path`,
  `status_code`, `duration_ms`, `client_ip`, `user_agent`), and responses carry an
  `X-Request-ID` header. `/api/health` is logged at DEBUG so healthchecks don't flood the logs.

To call protected routes from Swagger UI, run `login`, copy `access_token`, click
**Authorize**, and paste it.

### Workflows and executions

All routes require a Bearer token, and users only ever see their own workflows (anything else
is a `404`).

| Method | Path                                  | Description                                                        |
| ------ | ------------------------------------- | ------------------------------------------------------------------ |
| GET    | `/api/nodes`                          | The node catalog: type, category, group, label, description, icon, queue, branches, handles, output keys, and its config's JSON Schema |
| GET    | `/api/workflows`                      | List your workflows (newest first) with `node_count` and `last_execution` |
| POST   | `/api/workflows`                      | Create `{name, description}` with an empty graph                   |
| GET    | `/api/workflows/{id}`                 | One workflow, including its `graph`                                |
| PUT    | `/api/workflows/{id}`                 | Update `name` / `description` / `status`; `graph` fully replaces nodes, edges, and variables and bumps `version` |
| DELETE | `/api/workflows/{id}`                 | Delete the workflow and its execution history                      |
| POST   | `/api/workflows/{id}/validate`        | `{valid, errors: [...]}`; an empty list means the graph can run (includes `auth_missing` checks). Send `{graph}` to check unsaved edits |
| POST   | `/api/workflows/{id}/duplicate`       | Copy the workflow and its graph as "Name (copy)" → `201`           |
| POST   | `/api/workflows/{id}/nodes/{node_key}/test` | Run one saved node in isolation with `{config?, upstream_outputs, variables, inputs}` → `{status, input, output, error, duration_ms}`. Real providers: an LLM node calls the model and a Gmail node sends |
| POST   | `/api/workflows/{id}/run`             | Queue a run with `{inputs}` → `202 {execution_id, status: "pending", queue, links}`; `?sync=true` runs it in-request → `200` with the full execution; `503` if the broker is down |
| GET    | `/api/workflows/{id}/executions`      | Past executions, newest first (`limit`, `offset`)                  |
| GET    | `/api/executions`                     | All your executions, newest first, with `workflow_name` (`limit`, `offset`, `status`, `workflow_id`, `trigger`) |
| GET    | `/api/executions/{id}`                | One execution with every node's resolved input, output, and timing |
| GET    | `/api/executions/{id}/output`         | Download the final output: `?format=json` (default) or `csv` (one row per object in any list, e.g. per entity) |
| POST   | `/api/executions/{id}/stop`           | Stop a pending/running execution → `200` final state, `202` stop pending; `409` if finished |
| GET    | `/api/templates`                      | The template catalog with each requirement's `satisfied` / `using` and `ready` |
| POST   | `/api/templates/{slug}/use`           | `{timezone?}` → `201` a new workflow from the template (its triggers switched off) |
| …      | `/api/workflows/{id}/triggers`, …     | Triggers: see [Trigger API](#trigger-api) |
| WS     | `/ws/executions/{id}`                 | Snapshot + live events (see [Real-time events](#real-time-events-websocket)) |

Notes:

- **Saving vs running:** `PUT` stores any structurally valid graph, even one with unknown node
  types or cycles, so unfinished work can be saved. `/validate` reports the problems, and `/run`
  refuses an invalid graph with `422` without recording an execution. Duplicate node ids are
  rejected at save time because they can't be mapped to rows.
- **Run results:** a run that fails at a node ends with `status: "failed"` (a `?sync=true` run
  still returns `200`). The failing node's `error_message` explains why, and the nodes after it
  are `skipped` with the reason in their `error_message`. Node rows exist from the moment the
  run is queued (`pending`, in execution order via `position`).
- **Node rows and history:** `PUT` matches nodes to `workflow_nodes` rows by graph id. A node
  that survives a save keeps its row, and so its links in execution history. A removed node's
  history rows remain, with `node_id: null` and the `node_key`/`node_type`/`node_label`
  snapshot taken at run time.

### Files

| Method | Path                     | Description                                                          |
| ------ | ------------------------ | -------------------------------------------------------------------- |
| POST   | `/api/files`             | Upload (multipart field `file`) → `201 {id, filename, content_type, size_bytes, sha256, created_at}`; `413` over `MAX_UPLOAD_MB`, `415` not an allowed type, `411` no `Content-Length`, `400` empty |
| GET    | `/api/files`             | Your uploads, newest first                                           |
| GET    | `/api/files/{id}`        | One file's metadata                                                  |
| GET    | `/api/files/{id}/content`| The bytes, as an attachment with the detected type (`X-Content-Type-Options: nosniff`) |
| DELETE | `/api/files/{id}`        | Delete the file and its bytes                                        |

Owner-only (others get `404`). Use the `id` as the value of an Input node of type `file`.
Execution rows now also carry `segment` and `handoff_at`, node rows their `queue` and
`worker_hostname`, and node types in `GET /api/nodes` their `queue` and `portable`.

### Knowledge bases

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET / POST | `/api/knowledge-bases` | List yours (with `document_count`, `chunk_count`) / create one: `{name, description, embedding_provider, embedding_model?, chunk_size, chunk_overlap}` → `201`; `409` duplicate name, `422` provider not connected |
| GET / PATCH / DELETE | `/api/knowledge-bases/{id}` | One; rename or redescribe (the model and chunking can't change); delete with its documents and chunks (uploads stay under Files) |
| GET | `/api/knowledge-bases/{id}/documents` | Documents with `status` (pending/processing/ready/failed), `chunk_count`, `method`, `error` |
| POST | `/api/knowledge-bases/{id}/documents` | Upload (multipart `file`) → `202` pending, queued for ingestion; `415` not a PDF, image, or text; `503` queue down |
| POST | `/api/knowledge-bases/{id}/documents/from-file` | `{file_id}`: add an upload you already have → `202` |
| POST | `/api/knowledge-bases/{id}/documents/{doc}/retry` | Process a failed document again → `202` |
| DELETE | `/api/knowledge-bases/{id}/documents/{doc}` | Remove a document and its chunks |
| POST | `/api/knowledge-bases/{id}/search` | `{query, top_k}` → `results` with `rank`, `score`, `content`, `filename`, `page`, `chunk_id` |
| GET | `/api/knowledge-bases/{id}/chunks/{chunk_id}` | One chunk, its file, page, and offsets (following a citation) |

Owner-only (others get `404`); nodes find a knowledge base by its name or id among the run owner's.

### Deployments

| Method | Path | Auth | Description |
| ------ | ---- | ---- | ----------- |
| POST | `/api/deployments` | Bearer (JWT) | `{workflow_id}`: validate and deploy the saved graph → `201` with `api_key` (shown once); again → `200`, a redeploy of the current graph with the same id and key (`api_key: null`). `404` not yours, `422` invalid graph (`detail.errors`) |
| GET | `/api/deployments` | Bearer (JWT) | Your deployments, most recently deployed first (`?workflow_id=`): `endpoint`, `api_key_prefix`, `inputs`, `outputs`, `version`, `workflow_version`; never the key |
| POST | `/api/deployments/{id}/rotate-key` | Bearer (JWT) | A new key in `api_key`; the old one stops working at once. The deployed graph is unchanged |
| DELETE | `/api/deployments/{id}` | Bearer (JWT) | **Undeploy**: the endpoint answers `404` from now on, even with its key. The row is kept with `revoked_at` set (list it with `?include_revoked=true`), and its runs stay in the history. Deploying again reactivates it with a new key. Also the **Undeploy** button next to Deploy in the editor, after a confirmation |
| POST | `/api/v1/deployments/{id}/run` | API key | `{inputs}` → `202 {execution_id, status, links.status}`; `?wait=true[&timeout=s]` → `200` with `final_output` once finished (`202` at the timeout). `401`, `422`, `429`, `503` |
| GET | `/api/v1/deployments/{id}/executions/{execution_id}` | API key | A run this deployment started: `status`, `final_output`, `error`, timings. `404` for any other run |

The API-key routes take `Authorization: Bearer <key>` or `X-API-Key: <key>`. Details in
[Deploying a pipeline](#deploying-a-pipeline). Executions now also carry `deployment_id`.

### Integrations (credentials)

| Method | Path                                   | Description                                                        |
| ------ | -------------------------------------- | ------------------------------------------------------------------ |
| GET    | `/api/integrations`                    | Every provider: `connected` (you stored a credential), `source` (`user` / `server` / `none`: what a run would use), `status`, masked values, `last_test`, default model, where to get a key |
| POST   | `/api/integrations/{provider}/connect` | Store or replace your credential: `{api_key, model?}` for gemini/groq/openrouter/anthropic, `{api_key, base_url?, model?}` for openai, `{base_url?, model?}` for ollama, `{email, app_password, smtp_*?, imap_*?}` for gmail, `{bot_token, chat_id?}` for telegram, `{webhook_url}` for discord |
| DELETE | `/api/integrations/{provider}`         | Remove your credential (runs fall back to the server key, if any)  |
| POST   | `/api/integrations/{provider}/test`    | A real, minimal call with the credential a run would use: model metadata / list-models for LLMs (no tokens generated), SMTP + IMAP login for gmail, `getMe` + `getChat` for telegram, a `GET` of the webhook for discord (nothing is sent). Returns `{success, source, latency_ms, error, details}` |

- **Encryption at rest:** credential values are stored as Fernet ciphertext
  (`credentials.encrypted_value`) using `ENCRYPTION_KEY`. It may hold several comma-separated
  keys: the first encrypts, all decrypt, so you can rotate by prepending a new one.
- **Priority:** your credential beats the server-wide `.env` key; another user never sees or
  uses yours.
- **Never returned:** responses only show masked values (`"api_key": "AIz...9xQk"`,
  `"app_password": "********"`); validation errors on these routes don't echo the request body;
  and the JSON log formatter redacts every configured or decrypted secret.

**Try it in Swagger** (`/docs`): the request bodies are pre-filled.

1. `POST /api/auth/login` (demo user) → **Authorize** with the `access_token`.
2. `GET /api/integrations` → see which providers have a key (`source`). Optionally
   `POST /api/integrations/gemini/test` to check the key.
3. `POST /api/workflows` → copy the `id`.
4. `PUT /api/workflows/{id}` with the pre-filled Input → Gemini → Gmail → Output graph, and
   change the `recipient` variable to your own address.
5. `POST /api/workflows/{id}/validate` → `{"valid": true, "errors": []}`, or `auth_missing`
   errors naming what isn't configured.
6. `POST /api/workflows/{id}/run` with `{"inputs": {"topic": "..."}}` → `202` with the
   `execution_id`. `GET /api/executions/{execution_id}` a few seconds later shows
   `status: "success"`, `final_output.result.summary` is Gemini's real text, and
   `final_output.result.email` is the Gmail receipt (`status: "sent"`, a real `Message-ID`).
   The email arrives in the inbox. (Add `?sync=true` to get the finished execution directly.)
7. `GET /api/workflows/{id}/executions` → the run is listed.

## Frontend architecture

The web app ([`apps/web`](apps/web)) talks to the API directly from the browser
(`NEXT_PUBLIC_API_URL`) through the Phase 1 auth client, which refreshes the access token
before it expires and once on any `401`. TanStack Query holds server data; failed queries and
mutations show a toast unless they opt out with `meta: { silent: true }`.

- **Editor store** (`src/features/editor/store.ts`, Zustand): the graph (React Flow nodes and
  edges), variables, undo/redo snapshots, the save state machine, validation issues, and the
  live run state. React Flow is controlled, so every change goes through the store, where
  history and autosave see it.
- **Saving:** each edit bumps `revision`. `saveNow()` sends the latest graph and, if more edits
  landed while it was in flight, sends again. A session token drops responses for a workflow
  that's no longer loaded. `useAutosave` debounces it and retries with backoff after errors.
- **Forms:** `schema-form.ts` maps each property of a node's JSON Schema to a field kind and a
  Zod rule that mirrors Pydantic. A `{{reference}}` is accepted where a number or enum is
  expected, since it's resolved at run time. Backend issues for a field are merged in.
- **Runs:** `run-controller.tsx` queues a run and attaches an `ExecutionSocket`
  (`src/features/runs`). `run-state.ts` is a pure reducer from the snapshot and events
  (de-duplicated by `seq`) to per-node state; the editor and the execution detail page share it.
- **Files:** `src/features/files/file-chooser.tsx` picks one of your uploads or uploads a new
  one with a progress bar (`uploadFile` in `lib/api.ts` uses XHR, since fetch can't report
  upload progress). The run form, the Input node's default, and the document nodes' `file`
  field use it. `runs/output-view.tsx` shows node output readably (text blocks, entity
  lists, fact chips) with raw JSON on request.
- **Deploy:** `editor/deploy-dialog.tsx` saves, deploys, and rotates keys through
  `api.deployments`. The key it receives lives only in the dialog's state, so it's gone when
  the dialog closes. `editor/deploy.ts` has the pure parts (the Input/Output listing, example
  inputs, the curl command). `components/ui/truncated-text.tsx` is the one-line text with a
  full-text tooltip used for node summaries. It renders into `<body>` so other nodes can't
  cover it, and it stays readable at any zoom.
- **Triggers:** `editor/triggers-panel.tsx` (the panel; `useTriggers` also feeds the top
  bar's dot) and `editor/triggers.ts` (pure helpers). `features/dashboard/templates.tsx` is
  the Templates section, `components/ui/trigger-badge.tsx` the trigger badge, and
  `runs/output-downloads.tsx` the JSON/CSV buttons (`downloadAuthed` fetches with the token,
  since a plain link can't carry it).
- **Routes:** `/pipelines/[id]` (editor), `/dashboard`, `/executions`, `/executions/[id]`,
  `/integrations`, `/login`, and `/register`.

## Workflow engine

[`packages/workflow-engine`](packages/workflow-engine) is an installable Python package
(`flowforge_engine`) with no web or database dependencies; the API imports it. Its
[README](packages/workflow-engine/README.md) documents the graph format, the references, and
every node's config. In short:

- **Graph:** `{nodes: [{id, type, label, position, config}], edges: [{source, target, source_handle}], variables: [{key, value, type}]}`.
  Edges also accept React Flow's `sourceHandle`/`targetHandle`.
- **References:** `{{gemini.response}}` (an upstream node's output, with dot paths and `[0]`
  indexes), `{{input.topic}}` (an Input node's value), `{{vars.recipient}}` (a workflow
  variable), and `{{system.execution_id}}`. A config value that is exactly one reference keeps
  its type.
- **Nodes:** `input` (text, number, JSON, or file), `output`, `text`, `condition` (routes via
  `true`/`false` edge handles), `delay`, `gemini`, `groq`, `openrouter`, `ollama`, `openai`,
  `anthropic`, `gmail`, `gmail_read`, `http_request`, the document nodes `pdf_extract`,
  `ocr`, `summarize`, `extract_entities`, the list nodes `for_each`, `filter`, `join`, the
  sources `rss` and `web_page`, and `telegram` and `discord_webhook`.
  `default_registry.describe()` lists them with their JSON config schemas, `queue`,
  `portable`, and `item_fields` (per-item template fields, which the executor leaves for the
  node to resolve with `{{item}}` and `{{index}}`).
- **Node state:** `ExecutionServices.state` (`flowforge_engine.state.NodeStateStore`) keeps
  what a node remembers between runs; the API's store reads what the last successful run
  saved.
- **Queues:** each node type's `queue` (`default`, `llm`, `ocr`) says which workers run it, and
  `portable` nodes run anywhere. `execute_graph(..., accepts=..., completed=...)` pauses at
  the first node its caller can't run (result status `handoff`, `next_queue`) and resumes
  from the results so far (`flowforge_engine.routing`).
- **Files:** nodes read uploads through `ExecutionServices.files`, a `FileStore` the API scopes
  to the run's owner; `LocalFileStore` serves tests and standalone use.
- **Validation** catches unknown node types, missing or invalid config, broken edges, cycles
  (all cycles are rejected for now), unresolvable references (unknown nodes or variables,
  nodes that aren't upstream, and output keys a node doesn't produce), and, when given the
  run's `ExecutionServices`, providers without credentials (`auth_missing`).
- **Execution** runs nodes in topological order, resolving each node's config just before it
  runs. The first failure stops the run and skips the rest. Nodes behind a condition branch
  that wasn't taken are skipped without failing the run. Each node gets a timeout of
  `WORKFLOW_NODE_TIMEOUT_SECONDS`. `execute_graph(..., hooks=ExecutionHooks, control=ExecutionControl)`
  reports node starts, finishes, and LLM tokens to an observer (the worker's recorder) and
  supports cooperative stops (`control.request_stop(...)`: cancels an `interruptible` node,
  skips the rest; result status `stopped`). Node types declare `queue` and `interruptible`.
  Delay allows up to 60 s now that runs are off the request path.

**Providers** (see [Providers and free API keys](#providers-and-free-api-keys)): real
adapters only; a missing credential raises `MissingCredentialsError` ("Authentication
missing ..."). Adapter details:

- **Gemini:** google-genai against Google AI Studio; defaults to `gemini-3.5-flash-lite`,
  embeddings `gemini-embedding-2`. A `404` for a retired model (for example `gemini-2.5-flash`,
  which is no longer offered to new users) is passed through as-is. The SDK sends a streaming
  request lazily, so a `429`/`503` can surface on the first chunk; until any text has been
  handed out, that's retried with the same backoff as other calls.
- **OpenAI-compatible** (`OpenAICompatibleProvider`): one adapter with presets for Groq
  (`https://api.groq.com/openai/v1`), OpenRouter (`https://openrouter.ai/api/v1`, sends
  `max_tokens` and an `X-Title` header), Ollama (no key; `/test` checks that the model is
  pulled), and OpenAI (`temperature` omitted for reasoning models).
- **Anthropic:** defaults to `claude-opus-5` and drops `temperature` for current Claude models,
  which reject sampling parameters. For Opus 5 it enables server-side refusal fallbacks
  (`fallbacks: "default"`); above 16k `max_tokens` it streams internally.
- **Email:** `SMTPEmailProvider` (send) and `IMAPEmailProvider` (read), stdlib-based, run in a
  worker thread, with retries for connection problems and 4xx replies only (a dropped
  connection mid-send isn't retried, so a message is never sent twice).

## Database schema

All tables use UUID primary keys, `timestamptz` timestamps, and JSONB for JSON columns.

| Table                 | Notes                                                                             |
| --------------------- | --------------------------------------------------------------------------------- |
| `users`               | unique, indexed `email`                                                           |
| `workflows`           | `status` enum (draft/active/archived), `version`, `graph_json` (React Flow graph), trigger limits `max_runs_per_hour` and `max_consecutive_failures` |
| `workflow_nodes`      | `node_key` (the graph id, unique per workflow), `node_type`, `label`, position, `config_json` |
| `workflow_edges`      | source/target node FKs, optional handles                                          |
| `workflow_variables`  | `key`, `value`, `var_type` enum                                                   |
| `workflow_executions` | `status` and `trigger` enums, `created_at`, timings, `final_output_json`, `error_message`; for async runs `inputs_json` + `graph_json` (what was queued), `queue`, `celery_task_id`, `worker_hostname`, `heartbeat_at`, `stop_requested_at`; for queue hand-offs `segment` and `handoff_at`; `deployment_id` for runs started through a deployment (`SET NULL`); `trigger_id` for triggered runs (`SET NULL`) |
| `workflow_triggers`   | one per (workflow, `type` enum schedule/email/webhook): `enabled`, `config_json`, `state_json` (email: the mailbox position), `next_fire_at` (claimed with a compare-and-set), `last_fired_at`, `consecutive_failures`, `auto_disabled_at`, `disabled_reason`, `last_error` |
| `trigger_events`      | every fire time or Message-ID a trigger acted on, unique per (`trigger_id`, `event_key`), with its `outcome` (started / skipped / rejected / missed / duplicate) and `execution_id` |
| `node_states`         | what a node saved in a run (`workflow_id`, `node_key`, `execution_id`, `state_json`); readers take the newest from a successful run |
| `deployments`         | one per workflow (unique `workflow_id`): `owner_id`, the deployed `name` and `graph_json` snapshot, `workflow_version`, `version` (redeploys), `api_key_hash` (SHA-256 hex, never the key), `api_key_prefix`, `key_created_at`, `deployed_at` |
| `node_executions`     | per-node status, `position` (execution order), input/output JSON, timings, `duration_ms`, the `queue` and `worker_hostname` that ran it; `node_id` is nullable, plus a `node_key`/`node_type`/`node_label` snapshot |
| `credentials`         | per-user provider secrets: Fernet-encrypted JSON in `encrypted_value`; unique per (user, provider) |
| `files`               | uploads: `owner_id`, sanitized `filename`, detected `content_type`, `size_bytes`, `sha256`, `storage_key` (`<owner>/<id>` under `FILES_DIR`) |
| `integrations`        | per-user connection `status` enum and non-secret metadata (masked values, last test); unique per (user, provider) |
| `knowledge_bases`     | `owner_id`, `name` (unique per owner), `description`, `embedding_provider`, `embedding_model`, `dimensions` (768), `chunk_size`, `chunk_overlap` |
| `kb_documents`        | `knowledge_base_id`, `file_id` (`SET NULL`), `filename`, `status` enum (pending/processing/ready/failed), `source_type`, `method`, `chunk_count`, `char_count`, `error` |
| `kb_chunks`           | `document_id`, `knowledge_base_id`, `chunk_index`, `content`, `embedding vector(768)` (HNSW index, `vector_cosine_ops`), `metadata_json` (`page`, `start`, `end`) |
| `templates`           | `slug` (unique), `name`, `category`, starter `graph_json`, `requirements_json`, `triggers_json`, `sort_order`; synced from the catalog |

Child rows cascade on delete: deleting a workflow removes its nodes, edges, variables, and
executions. Deleting a single node does **not** delete its history: `node_executions.node_id`
is `ON DELETE SET NULL`, and the snapshot columns keep the row readable.
`workflow_executions.triggered_by_user_id` is set to NULL if that user is deleted.

Migrations: `initial schema` → `preserve node execution history` (node_id SET NULL +
snapshot) → `graph node keys` → `unique credential per provider` → `async execution columns` →
`files and queue handoff` → `deployments` → `triggers and templates` (which also adds
`email` to the trigger enum and renames deployment runs' trigger `api` to `webhook`) → `deployment revoked_at` → `knowledge bases (pgvector)` (enables the `vector` extension). The downgrade of the second one deletes history
rows whose node is gone, since those can't satisfy the old NOT NULL constraint. Deleting a
workflow deletes its deployment too.

## Environment variables

See [`.env.example`](.env.example) for the full list with comments. The main ones:

| Variable                        | Default                          | Used by              |
| ------------------------------- | -------------------------------- | -------------------- |
| `DATABASE_URL`                  | `postgresql+asyncpg://…@localhost:5433/flowforge` | API (local run) |
| `REDIS_URL`                     | `redis://localhost:6379/0`       | API + worker: broker, results, pub/sub |
| `JWT_SECRET`                    | none, **required** (≥ 32 chars)  | API                  |
| `ENCRYPTION_KEY`                | none, **required** (Fernet key; comma-separate to rotate) | API (stored credentials) |
| `JWT_ALGORITHM`                 | `HS256`                          | API                  |
| `ACCESS_TOKEN_EXPIRE_MINUTES`   | `30`                             | API                  |
| `REFRESH_TOKEN_EXPIRE_DAYS`     | `7`                              | API                  |
| `CORS_ORIGINS`                  | `http://localhost:3000,http://127.0.0.1:3000` | API     |
| `AUTH_RATE_LIMIT`               | `10/minute`                      | API                  |
| `DEPLOYMENT_RUN_RATE_LIMIT`, `DEPLOYMENT_STATUS_RATE_LIMIT` | `30/minute`, `240/minute` | API: per-deployment limits on runs and status polls |
| `DEPLOYMENT_WAIT_TIMEOUT_SECONDS`, `DEPLOYMENT_MAX_WAIT_SECONDS` | `30`, `120` | API: how long `?wait=true` waits by default / at most |
| `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_EMBEDDING_MODEL` | blank, `gemini-3.5-flash-lite`, `gemini-embedding-2` | Gemini nodes |
| `GROQ_API_KEY`, `GROQ_MODEL`    | blank, `openai/gpt-oss-20b`      | Groq nodes           |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | blank, `openrouter/free` | OpenRouter nodes     |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_EMBEDDING_MODEL` | `http://host.docker.internal:11434/v1` (`.env.example`), `llama3.2`, `nomic-embed-text` | Ollama nodes |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | blank, `gpt-4.1-mini`           | OpenAI nodes         |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | blank, `claude-opus-5`    | Claude nodes         |
| `MISTRAL_API_KEY`, `MISTRAL_MODEL` | blank, `ministral-8b-latest` | Mistral nodes      |
| `CEREBRAS_API_KEY`, `CEREBRAS_MODEL` | blank, `qwen-3.8-27b`       | Cerebras nodes       |
| `CUSTOM_OPENAI_BASE_URL`, `CUSTOM_OPENAI_API_KEY`, `CUSTOM_OPENAI_MODEL` | blank | Custom LLM nodes (base URL + model required) |
| `GROQ_WHISPER_MODEL`, `GROQ_WHISPER_TRANSLATE_MODEL`, `GROQ_WHISPER_MAX_FILE_MB` | `whisper-large-v3-turbo`, `whisper-large-v3`, `25` | Speech to Text on Groq |
| `FASTER_WHISPER_MODEL`, `FASTER_WHISPER_COMPUTE_TYPE`, `WHISPER_MODELS_DIR` | `base`, `int8`, `~/.cache/flowforge-whisper` (Docker: `/data/models`) | Speech to Text, local |
| `TAVILY_API_KEY`                | blank                            | Web Search (Tavily, and the DuckDuckGo fallback) |
| `NOTION_API_KEY`, `AIRTABLE_API_KEY` | blank                       | Notion and Airtable nodes (a Notion integration token, an Airtable personal access token) |
| `SMTP_USER`, `SMTP_PASSWORD`    | blank (Gmail address + App Password) | Gmail / Gmail Read nodes |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_FROM_NAME` | `smtp.gmail.com`, `587`, `auto`, blank | Gmail node |
| `IMAP_HOST`, `IMAP_PORT`        | `imap.gmail.com`, `993`          | Gmail Read node      |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | blank | Telegram node: the bot and the default chat |
| `DISCORD_WEBHOOK_URL`           | blank                            | Discord Webhook node |
| `TRIGGER_MISFIRE_GRACE_SECONDS` | `3600`                           | worker: a schedule seen later than this after its fire time is skipped |
| `EMAIL_TRIGGER_MIN_POLL_MINUTES`, `EMAIL_POLL_LOCK_SECONDS` | `1`, `300` | email triggers: the shortest poll interval; how long one poll may hold its lock |
| `LLM_MAX_RETRIES`, `LLM_RETRY_BASE_DELAY_SECONDS`, `LLM_RETRY_MAX_DELAY_SECONDS` | `3`, `1`, `30` | backoff for 429/5xx/network errors (LLMs, Telegram, Discord) |
| `LLM_REQUEST_TIMEOUT_SECONDS`   | `60`                             | per provider request |
| `WORKFLOW_NODE_TIMEOUT_SECONDS` | `120`                            | per-node limit during a run |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | blank (= `REDIS_URL`) | API + worker |
| `EXECUTION_TIME_LIMIT_SECONDS`  | `1800`                           | worker: whole-run limit (Celery limits +30/+60 s) |
| `CELERY_TASK_MAX_RETRIES`       | `3`                              | worker: retries for infrastructure errors before a run starts |
| `EXECUTION_HEARTBEAT_SECONDS`, `EXECUTION_STALE_AFTER_SECONDS` | `5`, `30` | crash detection |
| `EXECUTION_RECOVERY_INTERVAL_SECONDS` | `15`                       | API: stale-execution sweep |
| `EXECUTION_PENDING_TIMEOUT_SECONDS` | `3600`                       | pending runs no worker picked up |
| `EXECUTION_STOP_WAIT_SECONDS`, `EXECUTION_STOP_POLL_SECONDS` | `5`, `0.25` | stop endpoint wait; worker's stop-flag poll |
| `WORKER_DEFAULT_CONCURRENCY`, `WORKER_LLM_CONCURRENCY`, `WORKER_OCR_CONCURRENCY`, `WORKER_AUDIO_CONCURRENCY` | `4`, `8`, `2`, `2` | Compose: processes per worker container |
| `AUDIO_THREADS_PER_TASK`        | `2`                              | Compose `worker-audio`: CPU threads per local transcription |
| `OCR_THREADS_PER_TASK`          | `1`                              | Compose `worker-ocr`: `OMP_THREAD_LIMIT` for Tesseract |
| `FILES_DIR`                     | `apps/api/.data/files` (Docker: `/data/files`, the `files_data` volume) | API + workers: uploads |
| `MAX_UPLOAD_MB`                 | `25`                             | API: upload limit (documents, images) |
| `MAX_MEDIA_UPLOAD_MB`           | `500`                            | API: upload limit for audio and video |
| `SAMPLES_DIR`                   | the repo's `samples/` (Docker: `/samples`) | seed: the sample scan |
| `HTTP_ALLOW_PRIVATE_NETWORKS`   | `false`                          | HTTP Request, Web Page, Web Search, and Custom LLM: turn the SSRF guard off (development only) |
| `TESSERACT_LANGS` (build arg)   | `deu fra spa ita por` (plus `eng`) | image: OCR language data |
| `LOADTEST_INFLIGHT`, `LOADTEST_MIX`, `LOADTEST_LLM_PROVIDER` | `2`, `ocr`, `groq` | Compose `locust` (load test) |
| `WS_HEARTBEAT_SECONDS`, `WS_AUTH_TIMEOUT_SECONDS`, `WS_DB_CHECK_SECONDS` | `15`, `10`, `10` | WebSocket |
| `TESTING`                       | unset                            | set by the test suite only: all providers become mocks |
| `POSTGRES_USER/PASSWORD/DB`     | `flowforge`                      | Compose              |
| `*_HOST_PORT`                   | `5433`, `6379`, `8000`, `3000`   | Compose port mapping |
| `NEXT_PUBLIC_API_URL`           | `http://localhost:8000`          | Web                  |

Inside Compose, `DATABASE_URL` and `REDIS_URL` are overridden to use the `postgres` and
`redis` service hostnames, so the same `.env` works for both Docker and local runs. A blank
value (`GROQ_API_KEY=`) means "not set". Keep comments on their own lines in `.env`; not every
dotenv parser accepts trailing `# comments`.

## Project structure

```
.
├── compose.yaml                  # root entrypoint → includes infrastructure/docker-compose.yml
├── .env.example
├── infrastructure/
│   └── docker-compose.yml        # postgres, redis, api, worker-default/-llm/-ocr, beat, web; locust (profile)
├── apps/
│   ├── api/                      # FastAPI backend
│   │   ├── alembic.ini
│   │   ├── pytest.ini            # runs tests/ and the engine's tests
│   │   ├── requirements.txt      # pinned deps + the engine (editable)
│   │   ├── requirements-dev.txt  # + pytest
│   │   ├── Dockerfile
│   │   ├── tests/                # API tests (auth, workflows, execution, integrations, migrations; live)
│   │   └── app/
│   │       ├── main.py           # app, CORS, request logging, rate-limit handler
│   │       ├── core/             # config, security (bcrypt/JWT), crypto (Fernet), logging, rate limiter
│   │       ├── db/               # declarative base, async session, seed
│   │       ├── models/           # SQLAlchemy models + enums
│   │       ├── schemas/          # Pydantic request/response models
│   │       ├── services/         # graph sync, runs (create/claim/run/record), stop + recovery, events, task queue, credentials,
│   │       │                     #   triggers + schedule + trigger_outcomes, templates, node_state, exports (CSV)
│   │       ├── worker/           # Celery app (+ beat schedule) and tasks: runs, the triggers tick, email polls
│   │       ├── api/              # deps (get_current_user) + routes
│   │       └── alembic/          # env.py + versions/
│   ├── web/                      # Next.js frontend (see apps/web/README.md)
│   │   ├── src/app/              # routes: dashboard, pipelines/[id], executions, integrations, auth
│   │   ├── src/features/         # editor (canvas, store, forms, run panel), runs, dashboard, executions, integrations
│   │   ├── e2e/                  # Playwright end-to-end tests
│   │   └── playwright.config.ts
│   └── worker/                   # README only: the worker's code is apps/api/app/worker
├── docs/screenshots/             # written by the Playwright tests
├── loadtest/                     # Locust file, throughput script, suite runner, raw results
├── samples/                      # scanned-invoice.pdf (image-only) and the script that makes it
├── scripts/
│   ├── watch_run.py              # CLI: run a workflow and print its WebSocket events live
│   ├── run_document_demo.py      # CLI: run the document demo and show which worker ran each node
│   └── examples/                 # graphs for the CLI (gemini_gmail, gemini_output, delay, stream_tokens)
└── packages/
    ├── workflow-engine/          # flowforge_engine: registry, nodes, resolver, validator, executor, providers
    └── shared/                   # placeholder (shared types, later phase)
```

## Troubleshooting

- **Port already in use.** Change `POSTGRES_HOST_PORT`, `API_HOST_PORT`, etc. in `.env`. Postgres
  defaults to host port 5433 so it doesn't clash with a locally installed Postgres on 5432. If you
  move the API port, also update `NEXT_PUBLIC_API_URL`.
- **Web can't reach the API / CORS errors.** The browser calls the API directly at
  `NEXT_PUBLIC_API_URL`, and the page's origin must be listed in `CORS_ORIGINS`.
- **Hot reload in Docker.** Docker Desktop bind mounts don't deliver file-change events, so
  both dev servers poll: `WATCHFILES_FORCE_POLLING` for uvicorn, and `NEXT_DEV_POLL_INTERVAL_MS`
  plus the webpack dev server for Next.js. Turbopack's poll watcher misses changes made through
  bind mounts. `npm run dev` on the host still uses Turbopack.
- **A new page returns 404 in Docker.** The webpack watcher can miss a new route folder under
  `src/app/`; run `docker compose restart web`.
- **`429 Too many requests` while testing.** Wait a minute, or raise `AUTH_RATE_LIMIT` in `.env`
  and restart the API.
- **`auth_missing` / "Authentication missing for provider ..."** The node's provider has no key
  (neither yours nor the server's). Add it to `.env` and restart the API, or connect it with
  `POST /api/integrations/{provider}/connect`. For a deliberate dry run, set the node's
  `provider` (or an email node's `auth`) to `mock`.
- **Gmail `534 5.7.9 Application-specific password required`.** `SMTP_PASSWORD` is your normal
  password; create an App Password (see [Gmail: create an App Password](#gmail-create-an-app-password)).
- **Gemini `429 ... quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier=N`.** The
  free daily quota for that model is used up. Wait for the reset, switch `GEMINI_MODEL` to a
  model with more free quota, or add a `fallback` chain.
- **Gemini `503 ... currently experiencing high demand` (or `504`).** Google is overloaded;
  it passes. A `fallback` chain (the seeded pipelines use Groq when `GROQ_API_KEY` is set)
  answers meanwhile.
- **E2E: `"Demo: ..." must validate before these tests can run it`.** A seeded pipeline was
  edited into a state that can't run, often a node added by accident (a stray click in the
  library adds one at the center). The message lists the backend's reasons. Fix it in the
  editor, or delete the pipeline and run the seed again.
- **Runs stay `pending`, or `running` with "Handed off: waiting for a ... worker".** No worker
  consumes that queue: `docker compose ps` (is `worker-ocr` / `worker-llm` / `worker-default`
  up and healthy?), then `docker compose logs worker-ocr`. After
  `EXECUTION_PENDING_TIMEOUT_SECONDS` they're marked failed.
- **OCR fails with "Tesseract language data not installed".** Rebuild with the language:
  `docker compose build --build-arg TESSERACT_LANGS="deu fra hin" api worker-ocr`.
- **An HTTP Request node fails with "Blocked request to ...".** That's the
  [SSRF guard](#security-outbound-requests-ssrf-guard). For a service on your own machine
  during development, set `HTTP_ALLOW_PRIVATE_NETWORKS=true` and restart the workers.
- **A run was marked failed with "No heartbeat from worker ..." or "... restarted while this
  execution was running".** The worker was killed, crashed, or was restarted (including a
  `watchfiles` reload after a code change) mid-run; that's crash recovery doing its job.
- **WebSocket closes with 4401 / 4404.** 4401: send `{"type": "auth", "token": ...}` first (or
  `?token=`) with a current *access* token (not the refresh token). 4404: the execution id is
  wrong or belongs to another user.
- **Ollama "cannot connect ... is `ollama serve` running?"** Start Ollama on the host. From
  Docker, `OLLAMA_BASE_URL` must be `http://host.docker.internal:11434/v1`, not `localhost`.
  "model is not pulled" means you need to run `ollama pull <model>`.
- **API won't start: `ENCRYPTION_KEY` missing or invalid.** Generate one (see Quick start). If
  you replace the key, stored credentials can't be decrypted and are ignored (with a warning)
  until users reconnect them. Prepend the new key instead (`NEW,OLD`) to rotate.
- **Containers exit with code 255 after the machine sleeps.** Docker Desktop's file sharing can
  drop bind mounts (`EIO` errors in the logs). `api` and `web` use `restart: unless-stopped` and
  come back on their own; if not, run `docker compose up -d`.
- **Security note: per-user LLM endpoints.** The HTTP Request node is guarded (see
  [SSRF guard](#security-outbound-requests-ssrf-guard)), but the per-user `base_url` accepted
  for Ollama and OpenAI credentials isn't yet: add the same check before exposing the API to
  untrusted users.
