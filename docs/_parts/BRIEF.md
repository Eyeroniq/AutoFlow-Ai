# Brief for documentation agents (temporary file; deleted after assembly)

The user wants a complete learning guide / technical-interview preparation for the FlowForge AI project (the user calls it
"AutoFlow"; the repo is at the working directory, GitHub Eyeroniq/AutoFlow-Ai). Three final documents will be assembled from
parts that agents write in `docs/_parts/`. You write ONE part. Everything must come from reading the ACTUAL source code.

## Non-negotiable rules
1. **Verify from source.** Read the code before claiming anything. Never describe behaviour from the UI alone or from the README
   alone (the README can be stale or aspirational). If code and README disagree, code wins; say so.
2. **Mark uncertainty explicitly** with exactly one of these tags: `NOT IMPLEMENTED`, `IMPLEMENTED BUT NOT CURRENTLY CONNECTED`,
   `INFERRED FROM CODE`. Do not invent functionality, endpoints, fields, or env vars. If you are not sure a name is right, open the file.
3. **Plain language first, then technical.** For every concept: one or two sentences a non-programmer would understand ("In simple
   terms: ..."), followed by the technical explanation with real file paths, function/class names, routes, table/column names.
4. **Be exhaustive and concrete.** Use real identifiers (`apps/api/app/services/runs.py::create_execution`), real routes
   (`POST /api/workflows/{workflow_id}/run`), real model/table names, real config field names with their types/defaults/constraints.
   No filler, no marketing language, no generic "AI workflow tool" paragraphs.
5. **Write Markdown** with headings, tables where they help (field tables, route tables), fenced code blocks for flow diagrams,
   and Mermaid diagrams where useful. Use relative-style paths from the repo root in backticks.
6. **Length:** this is a reference document; thorough is the goal. Typically 1,500-4,000 lines for your part is fine. Do not stop
   early because the output is long; split your writing into several Write/Edit calls appending sections if needed (write the file
   in chunks: create it with the first sections, then append with further Edit/Bash `cat >>`-style steps - prefer the Write tool
   for the first chunk and Edit to append).
7. Do NOT modify any source code. Only create your own part file under `docs/_parts/`. Do not run docker or tests. Reading with
   Read/Grep/Glob is enough (Bash for `wc`/`ls` only).
8. At the end of your part add a short section `## Verification notes` listing anything you could not verify and any place where the
   README/UI text disagrees with the code.
9. Use they/them for people. Do not include secrets (none should be in the code anyway; never print values from `.env`).

## Repository map (starting points)
- `apps/web/` Next.js 16 App Router frontend (`src/app`, `src/features/*`, `src/components`, `src/lib`, `src/proxy.ts`).
- `apps/api/` FastAPI backend (`app/main.py`, `app/api/routes`, `app/services`, `app/models`, `app/schemas`, `app/core`, `app/db`,
  `app/worker`, `app/alembic/versions`), bots: `app/discord_bot.py`, `app/discord_controller.py`, `app/telegram_listener.py`.
- `apps/discord-recorder/` Node.js voice recorder service. `apps/worker/` (docs only for the Celery workers).
- `packages/workflow-engine/` the engine (`src/flowforge_engine`: `executor.py`, `graph.py`, `variables.py`, `registry.py`, `nodes/*`,
  `providers/*`, `services.py`, `routing.py`, ...). `packages/shared/` generated TS API types.
- `infrastructure/docker-compose.yml` (+ root `compose.yaml`), `compose.prod.yaml`, `infrastructure/Caddyfile`, `scripts/`, `.github/workflows/ci.yml`.
- `docs/HOSTING.md`, `README.md` (use for orientation only).

## Terminology to keep consistent across all parts
- "Pipeline" = "workflow" in the UI (route `/pipelines/{id}`); API/DB call it workflow. Say both once, then pick "workflow".
- "Node" = a block; "edge" = connection; "graph" = nodes + edges + variables stored as JSON (`workflows.graph_json`).
- "Run" = one execution (`workflow_executions` row); "step" = one node's execution (`node_executions`).
- "Deployment" = a published copy with an API key and endpoint (`deployments` table), NOT "activating" the workflow.
- Triggers (schedule, email, webhook, telegram, and the Discord voice watcher) are separate from deployments; know the difference.

Your specific assignment follows in the prompt you were given.
