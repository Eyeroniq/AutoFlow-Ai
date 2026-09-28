# FlowForge AI

A visual AI workflow automation builder. This repository is being built in phases.

- **Phase 1:** monorepo skeleton, Docker Compose stack, the full database schema, JWT
  authentication (with refresh), and a minimal Next.js frontend that proves auth end to end.
- **Phase 2:** the workflow engine package (node registry, `{{...}}` variable resolution,
  graph validation, execution) and workflow CRUD + synchronous execution APIs.
- **Phase 2.5 (this state):** real, free-tier-friendly providers instead of mocks: Google
  Gemini, Groq, OpenRouter (`:free` models), and local Ollama for LLMs (plus OpenAI and
  Anthropic if you add keys), and Gmail over SMTP/IMAP with an App Password. Per-user
  credentials are encrypted at rest, and a provider without credentials is a validation
  error, never a silent mock. Everything is testable from `/docs`; the frontend is unchanged.

The canvas, Celery worker, WebSockets, and templates come in later phases.

## Stack

| Layer    | Tech                                                                        |
| -------- | --------------------------------------------------------------------------- |
| Frontend | Next.js 16 (App Router), TypeScript, Tailwind CSS v4, React Hook Form + Zod |
| Backend  | Python 3.13, FastAPI, SQLAlchemy 2.0 (async, asyncpg), Alembic, Pydantic v2 |
| Engine   | `packages/workflow-engine` (Pydantic v2, httpx); google-genai, openai, anthropic SDKs; stdlib smtplib/imaplib |
| Secrets  | Fernet (`cryptography`) for stored credentials                                |
| Auth     | JWT (python-jose, HS256), passlib + bcrypt, slowapi rate limiting           |
| Data     | PostgreSQL 16, Redis 7 (provisioned but not used yet)                       |
| Tests    | pytest + pytest-asyncio, against a real Postgres test database              |
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

Log in with **demo@flowforge.ai** / **demo1234**, or register a new account.

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
3. `web` starts `next dev` once the API healthcheck passes.

Both `api` and `web` bind-mount their source directories, so edits hot-reload. The API
container also mounts `packages/workflow-engine` (installed editable), so engine edits
reload it too.

Useful commands:

```bash
docker compose up -d                   # run in the background
docker compose logs -f api             # follow API logs (structured JSON)
docker compose ps                      # service status and health
docker compose down                    # stop (keeps the database volume)
docker compose down -v                 # stop and delete all data
docker compose up --build -V web       # after changing package.json (renews node_modules volume)
docker compose up --build api          # after changing requirements.txt or the engine's dependencies
```

## Tests

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

Locally (venv, with the dockerized Postgres running): `cd apps/api && pytest`.

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
`demo@flowforge.ai` / `demo1234`. It's idempotent, so it's safe to run more than once.

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
```

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
| Groq         | `groq`       | OpenAI-compatible        | `llama-3.3-70b-versatile`            | `GROQ_API_KEY` (free)    |
| OpenRouter   | `openrouter` | OpenAI-compatible        | `openrouter/free` (any `…:free` model works) | `OPENROUTER_API_KEY` (free) |
| Ollama       | `ollama`     | OpenAI-compatible        | `llama3.2`                           | none (local)             |
| OpenAI       | `openai`     | OpenAI-compatible        | `gpt-4.1-mini`                       | `OPENAI_API_KEY` (paid)  |
| Anthropic    | `anthropic`  | anthropic SDK            | `claude-opus-5`                      | `ANTHROPIC_API_KEY` (paid) |

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
Providers in the chain are credential-checked at validation time too.

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
| GET    | `/api/workflows`                      | List your workflows (newest first)                                 |
| POST   | `/api/workflows`                      | Create `{name, description}` with an empty graph                   |
| GET    | `/api/workflows/{id}`                 | One workflow, including its `graph`                                |
| PUT    | `/api/workflows/{id}`                 | Update `name` / `description` / `status`; `graph` fully replaces nodes, edges, and variables and bumps `version` |
| DELETE | `/api/workflows/{id}`                 | Delete the workflow and its execution history                      |
| POST   | `/api/workflows/{id}/validate`        | `{valid, errors: [...]}`; an empty list means the graph can run (includes `auth_missing` checks) |
| POST   | `/api/workflows/{id}/run`             | Run synchronously with `{inputs}`; returns the full execution      |
| GET    | `/api/workflows/{id}/executions`      | Past executions, newest first (`limit`, `offset`)                  |
| GET    | `/api/executions/{id}`                | One execution with every node's resolved input, output, and timing |

Notes:

- **Saving vs running:** `PUT` stores any structurally valid graph, even one with unknown node
  types or cycles, so unfinished work can be saved. `/validate` reports the problems, and `/run`
  refuses an invalid graph with `422` without recording an execution. Duplicate node ids are
  rejected at save time because they can't be mapped to rows.
- **Run results:** a run that fails at a node still returns `200` with `status: "failed"`. The
  failing node's `error_message` explains why, and the nodes after it are `skipped` with the
  reason in their `error_message`.
- **Node rows and history:** `PUT` matches nodes to `workflow_nodes` rows by graph id. A node
  that survives a save keeps its row, and so its links in execution history. A removed node's
  history rows remain, with `node_id: null` and the `node_key`/`node_type`/`node_label`
  snapshot taken at run time.

### Integrations (credentials)

| Method | Path                                   | Description                                                        |
| ------ | -------------------------------------- | ------------------------------------------------------------------ |
| GET    | `/api/integrations`                    | Every provider: `connected` (you stored a credential), `source` (`user` / `server` / `none`: what a run would use), `status`, masked values, `last_test`, default model, where to get a key |
| POST   | `/api/integrations/{provider}/connect` | Store or replace your credential: `{api_key, model?}` for gemini/groq/openrouter/anthropic, `{api_key, base_url?, model?}` for openai, `{base_url?, model?}` for ollama, `{email, app_password, smtp_*?, imap_*?}` for gmail |
| DELETE | `/api/integrations/{provider}`         | Remove your credential (runs fall back to the server key, if any)  |
| POST   | `/api/integrations/{provider}/test`    | A real, minimal call with the credential a run would use: model metadata / list-models for LLMs (no tokens generated), SMTP + IMAP login for gmail. Returns `{success, source, latency_ms, error, details}` |

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
6. `POST /api/workflows/{id}/run` with `{"inputs": {"topic": "..."}}` → `status: "success"`,
   `final_output.result.summary` is Gemini's real text, and `final_output.result.email` is the
   Gmail receipt (`status: "sent"`, a real `Message-ID`). The email arrives in the inbox.
7. `GET /api/workflows/{id}/executions` → the run is listed.

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
- **Nodes:** `input`, `output`, `text`, `condition` (routes via `true`/`false` edge handles),
  `delay` (≤ 10 s), `gemini`, `groq`, `openrouter`, `ollama`, `openai`, `anthropic`, `gmail`,
  `gmail_read`, `http_request`. `default_registry.describe()` lists them with their JSON config
  schemas.
- **Validation** catches unknown node types, missing or invalid config, broken edges, cycles
  (all cycles are rejected for now), unresolvable references (unknown nodes or variables,
  nodes that aren't upstream, and output keys a node doesn't produce), and, when given the
  run's `ExecutionServices`, providers without credentials (`auth_missing`).
- **Execution** runs nodes in topological order, resolving each node's config just before it
  runs. The first failure stops the run and skips the rest. Nodes behind a condition branch
  that wasn't taken are skipped without failing the run. Each node gets a timeout of
  `WORKFLOW_NODE_TIMEOUT_SECONDS`.

**Providers** (see [Providers and free API keys](#providers-and-free-api-keys)): real
adapters only; a missing credential raises `MissingCredentialsError` ("Authentication
missing ..."). Adapter details:

- **Gemini:** google-genai against Google AI Studio; defaults to `gemini-3.5-flash-lite`,
  embeddings `gemini-embedding-2`. A `404` for a retired model (for example `gemini-2.5-flash`,
  which is no longer offered to new users) is passed through as-is.
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
| `workflows`           | `status` enum (draft/active/archived), `version`, `graph_json` (React Flow graph) |
| `workflow_nodes`      | `node_key` (the graph id, unique per workflow), `node_type`, `label`, position, `config_json` |
| `workflow_edges`      | source/target node FKs, optional handles                                          |
| `workflow_variables`  | `key`, `value`, `var_type` enum                                                   |
| `workflow_executions` | `status` and `trigger` enums, timings, `final_output_json`, `error_message`       |
| `node_executions`     | per-node status, input/output JSON, timings, `duration_ms`; `node_id` is nullable, plus a `node_key`/`node_type`/`node_label` snapshot |
| `credentials`         | per-user provider secrets: Fernet-encrypted JSON in `encrypted_value`; unique per (user, provider) |
| `integrations`        | per-user connection `status` enum and non-secret metadata (masked values, last test); unique per (user, provider) |
| `templates`           | `name`, `category`, starter `graph_json`                                          |

Child rows cascade on delete: deleting a workflow removes its nodes, edges, variables, and
executions. Deleting a single node does **not** delete its history: `node_executions.node_id`
is `ON DELETE SET NULL`, and the snapshot columns keep the row readable.
`workflow_executions.triggered_by_user_id` is set to NULL if that user is deleted.

Migrations: `initial schema` → `preserve node execution history` (node_id SET NULL +
snapshot) → `graph node keys` → `unique credential per provider`. The downgrade of the second one deletes history rows whose
node is gone, since those can't satisfy the old NOT NULL constraint.

## Environment variables

See [`.env.example`](.env.example) for the full list with comments. The main ones:

| Variable                        | Default                          | Used by              |
| ------------------------------- | -------------------------------- | -------------------- |
| `DATABASE_URL`                  | `postgresql+asyncpg://…@localhost:5433/flowforge` | API (local run) |
| `REDIS_URL`                     | `redis://localhost:6379/0`       | API (not used yet)   |
| `JWT_SECRET`                    | none, **required** (≥ 32 chars)  | API                  |
| `ENCRYPTION_KEY`                | none, **required** (Fernet key; comma-separate to rotate) | API (stored credentials) |
| `JWT_ALGORITHM`                 | `HS256`                          | API                  |
| `ACCESS_TOKEN_EXPIRE_MINUTES`   | `30`                             | API                  |
| `REFRESH_TOKEN_EXPIRE_DAYS`     | `7`                              | API                  |
| `CORS_ORIGINS`                  | `http://localhost:3000,http://127.0.0.1:3000` | API     |
| `AUTH_RATE_LIMIT`               | `10/minute`                      | API                  |
| `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_EMBEDDING_MODEL` | blank, `gemini-3.5-flash-lite`, `gemini-embedding-2` | Gemini nodes |
| `GROQ_API_KEY`, `GROQ_MODEL`    | blank, `llama-3.3-70b-versatile` | Groq nodes           |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | blank, `openrouter/free` | OpenRouter nodes     |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_EMBEDDING_MODEL` | `http://host.docker.internal:11434/v1` (`.env.example`), `llama3.2`, `nomic-embed-text` | Ollama nodes |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | blank, `gpt-4.1-mini`           | OpenAI nodes         |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | blank, `claude-opus-5`    | Claude nodes         |
| `SMTP_USER`, `SMTP_PASSWORD`    | blank (Gmail address + App Password) | Gmail / Gmail Read nodes |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_FROM_NAME` | `smtp.gmail.com`, `587`, `auto`, blank | Gmail node |
| `IMAP_HOST`, `IMAP_PORT`        | `imap.gmail.com`, `993`          | Gmail Read node      |
| `LLM_MAX_RETRIES`, `LLM_RETRY_BASE_DELAY_SECONDS`, `LLM_RETRY_MAX_DELAY_SECONDS` | `3`, `1`, `30` | backoff for 429/5xx/network errors |
| `LLM_REQUEST_TIMEOUT_SECONDS`   | `60`                             | per provider request |
| `WORKFLOW_NODE_TIMEOUT_SECONDS` | `120`                            | API (per-node limit during a run) |
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
│   └── docker-compose.yml        # postgres, redis, api, web
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
│   │       ├── services/         # graph sync, run + persist, credentials + per-user providers
│   │       ├── api/              # deps (get_current_user) + routes
│   │       └── alembic/          # env.py + versions/
│   ├── web/                      # Next.js frontend (see apps/web/README.md)
│   └── worker/                   # placeholder (Celery worker, later phase)
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
- **Ollama "cannot connect ... is `ollama serve` running?"** Start Ollama on the host. From
  Docker, `OLLAMA_BASE_URL` must be `http://host.docker.internal:11434/v1`, not `localhost`.
  "model is not pulled" means you need to run `ollama pull <model>`.
- **API won't start: `ENCRYPTION_KEY` missing or invalid.** Generate one (see Quick start). If
  you replace the key, stored credentials can't be decrypted and are ignored (with a warning)
  until users reconnect them. Prepend the new key instead (`NEW,OLD`) to rotate.
- **Containers exit with code 255 after the machine sleeps.** Docker Desktop's file sharing can
  drop bind mounts (`EIO` errors in the logs). `api` and `web` use `restart: unless-stopped` and
  come back on their own; if not, run `docker compose up -d`.
- **Security note: HTTP Request node.** It can call any URL the API container can reach,
  including internal addresses like `http://postgres:5432`. That's fine for local development,
  but add an allow/deny list before exposing the API to untrusted users. The same applies to
  the per-user `base_url` accepted for Ollama and OpenAI credentials.
