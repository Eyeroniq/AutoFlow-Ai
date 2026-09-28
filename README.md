# FlowForge AI

A visual AI workflow automation builder. This repository is being built in phases.

- **Phase 1:** monorepo skeleton, Docker Compose stack, the full database schema, JWT
  authentication (with refresh), and a minimal Next.js frontend that proves auth end to end.
- **Phase 2 (this state):** the workflow engine package (node registry, `{{...}}` variable
  resolution, graph validation, execution), LLM/email provider abstractions with mock
  fallbacks, and workflow CRUD + synchronous execution APIs. Everything is testable from
  `/docs`; the frontend is unchanged apart from automatic token refresh.

The canvas, Celery worker, WebSockets, templates, and real integrations come in later phases.

## Stack

| Layer    | Tech                                                                        |
| -------- | --------------------------------------------------------------------------- |
| Frontend | Next.js 16 (App Router), TypeScript, Tailwind CSS v4, React Hook Form + Zod |
| Backend  | Python 3.13, FastAPI, SQLAlchemy 2.0 (async, asyncpg), Alembic, Pydantic v2 |
| Engine   | `packages/workflow-engine` (Pydantic v2, httpx); google-genai, openai, anthropic SDKs |
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

# 2. Set a real JWT secret in .env (any long random string), e.g.:
python -c "import secrets; print(secrets.token_urlsafe(64))"

# 3. Boot everything (first run builds the images)
docker compose up --build

# 4. In another terminal, create the demo user
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
```

API tests run against a real Postgres: a `flowforge_test` database is dropped, recreated, and
migrated with Alembic at the start of each run, and every test is rolled back afterwards. The
dev database is never touched. Provider API keys are blanked for the run, so every LLM call
uses the mock provider. `test_migrations.py` also round-trips every migration
(upgrade → downgrade → upgrade) on a scratch database and checks it matches the models.

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
| POST   | `/api/workflows/{id}/validate`        | `{valid, errors: [...]}`; an empty list means the graph can run    |
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

**Try it in Swagger** (`/docs`): the request bodies are pre-filled.

1. `POST /api/auth/login` (demo user) → **Authorize** with the `access_token`.
2. `POST /api/workflows` → copy the `id`.
3. `PUT /api/workflows/{id}` with the pre-filled Input → Gemini → Gmail → Output graph.
4. `POST /api/workflows/{id}/validate` → `{"valid": true, "errors": []}`.
5. `POST /api/workflows/{id}/run` with `{"inputs": {"topic": "..."}}` → `status: "success"`,
   `final_output.result.summary` is the mock Gemini text, and `final_output.result.email` is
   the mock Gmail receipt (`status: "sent"`, `message_id: "mock-…"`).
6. `GET /api/workflows/{id}/executions` → the run is listed.

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
  `delay` (≤ 10 s), `gemini`, `openai`, `anthropic`, `gmail`, `http_request`.
  `default_registry.describe()` lists them with their JSON config schemas.
- **Validation** catches unknown node types, missing or invalid config, broken edges, cycles
  (all cycles are rejected for now), and unresolvable references: unknown nodes or variables,
  nodes that aren't upstream, and output keys a node doesn't produce.
- **Execution** runs nodes in topological order, resolving each node's config just before it
  runs. The first failure stops the run and skips the rest. Nodes behind a condition branch
  that wasn't taken are skipped without failing the run. Each node gets a timeout of
  `WORKFLOW_NODE_TIMEOUT_SECONDS`.

**Providers:** each LLM node uses the real SDK adapter when its key (`GEMINI_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) is set, and otherwise falls back to a deterministic
mock (`[MOCK RESPONSE to: <first 50 chars>]`) with a warning in the logs. Email always uses
the mock in this phase: nothing is sent, and the receipt (a `mock-…` message id) is recorded in
the execution history. Adapter details:

- **Anthropic:** defaults to `claude-opus-5` and drops `temperature` for current Claude models,
  which reject sampling parameters. For Opus 5 it enables server-side refusal fallbacks
  (`fallbacks: "default"`).
- **OpenAI:** defaults to `gpt-4.1-mini`; `temperature` is omitted for reasoning models.
- **Gemini:** defaults to `gemini-2.5-flash`.

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
| `credentials`         | per-user provider secrets (`encrypted_value`; encryption lands in a later phase)  |
| `integrations`        | per-user provider connection `status` enum and metadata                           |
| `templates`           | `name`, `category`, starter `graph_json`                                          |

Child rows cascade on delete: deleting a workflow removes its nodes, edges, variables, and
executions. Deleting a single node does **not** delete its history: `node_executions.node_id`
is `ON DELETE SET NULL`, and the snapshot columns keep the row readable.
`workflow_executions.triggered_by_user_id` is set to NULL if that user is deleted.

Migrations: `initial schema` → `preserve node execution history` (node_id SET NULL +
snapshot) → `graph node keys`. The downgrade of the second one deletes history rows whose
node is gone, since those can't satisfy the old NOT NULL constraint.

## Environment variables

See [`.env.example`](.env.example) for the full list with comments. The main ones:

| Variable                        | Default                          | Used by              |
| ------------------------------- | -------------------------------- | -------------------- |
| `DATABASE_URL`                  | `postgresql+asyncpg://…@localhost:5433/flowforge` | API (local run) |
| `REDIS_URL`                     | `redis://localhost:6379/0`       | API (not used yet)   |
| `JWT_SECRET`                    | none, **required** (≥ 32 chars)  | API                  |
| `JWT_ALGORITHM`                 | `HS256`                          | API                  |
| `ACCESS_TOKEN_EXPIRE_MINUTES`   | `30`                             | API                  |
| `REFRESH_TOKEN_EXPIRE_DAYS`     | `7`                              | API                  |
| `CORS_ORIGINS`                  | `http://localhost:3000,http://127.0.0.1:3000` | API     |
| `AUTH_RATE_LIMIT`               | `10/minute`                      | API                  |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | blank (mock mode) | API (LLM nodes) |
| `WORKFLOW_NODE_TIMEOUT_SECONDS` | `120`                            | API (per-node limit during a run) |
| `POSTGRES_USER/PASSWORD/DB`     | `flowforge`                      | Compose              |
| `*_HOST_PORT`                   | `5433`, `6379`, `8000`, `3000`   | Compose port mapping |
| `NEXT_PUBLIC_API_URL`           | `http://localhost:8000`          | Web                  |

Inside Compose, `DATABASE_URL` and `REDIS_URL` are overridden to use the `postgres` and
`redis` service hostnames, so the same `.env` works for both Docker and local runs.

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
│   │   ├── tests/                # API tests (auth, workflows, execution, history, migrations)
│   │   └── app/
│   │       ├── main.py           # app, CORS, request logging, rate-limit handler
│   │       ├── core/             # config, security (bcrypt/JWT), logging, rate limiter
│   │       ├── db/               # declarative base, async session, seed
│   │       ├── models/           # SQLAlchemy models + enums
│   │       ├── schemas/          # Pydantic request/response models
│   │       ├── services/         # graph sync, run + persist, provider wiring
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
- **Containers exit with code 255 after the machine sleeps.** Docker Desktop's file sharing can
  drop bind mounts (`EIO` errors in the logs). `api` and `web` use `restart: unless-stopped` and
  come back on their own; if not, run `docker compose up -d`.
- **Security note: HTTP Request node.** It can call any URL the API container can reach,
  including internal addresses like `http://postgres:5432`. That's fine for local development,
  but add an allow/deny list before exposing the API to untrusted users.
