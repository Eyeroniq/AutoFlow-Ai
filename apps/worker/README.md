# FlowForge AI: workers and background services

There is no code in this folder. The Celery worker lives in the API package,
[`apps/api/app/worker`](../api/app/worker), because it uses the same models, settings, credential
resolution, and run service as the API (`app.services.runs.run_execution`). Compose runs it from the
API image, once per queue, so each kind of work scales on its own.

## Workers (one Compose service per queue)

| Service | Queue | Concurrency (env, default) | Runs these nodes |
| --- | --- | --- | --- |
| `worker-default` | `default` | `WORKER_DEFAULT_CONCURRENCY` (4) | Input, Output, Text, Condition, Filter, Join, Delay, Chunker, HTTP Request, Web Page, Web Search, RSS, Gmail, Gmail Read, Telegram, Discord Webhook, Notion Create Page / Query Database, Airtable Create / List Records, ICS Calendar, Secret Scanner, PII Redact / Restore; the triggers tick and email polls |
| `worker-llm` | `llm` | `WORKER_LLM_CONCURRENCY` (8) | every LLM node (Gemini, Groq, OpenRouter, Mistral, Cerebras, Ollama, OpenAI, Anthropic, Custom), Vision, Structured Output, Summarize, Entity Extraction, For Each, Embedding, Retriever, Reranker; resume refinement runs |
| `worker-ocr` | `ocr` | `WORKER_OCR_CONCURRENCY` (2) | PDF Extract, OCR, Redact Image, Add to Knowledge Base; knowledge-base ingestion. CPU-bound: `OMP_THREAD_LIMIT` is `OCR_THREADS_PER_TASK` (1) |
| `worker-audio` | `audio` | `WORKER_AUDIO_CONCURRENCY` (2) | Speech to Text (Groq Whisper, or faster-whisper on the CPU with `OMP_NUM_THREADS` = `AUDIO_THREADS_PER_TASK`, 2). Mounts the `whisper_models` volume |

Each is

```bash
celery -A app.worker.celery_app:celery_app worker --hostname=<service>@%h --queues=<queue> --concurrency=<n>
```

run under `watchfiles` in the dev Compose file, so a change to `app/` or the engine restarts it. The
queue of a node is its `queue` attribute in the engine (`flowforge_engine.routing`); a run **hands itself
from queue to queue** as it reaches each kind of node, so a graph that reads a PDF and then calls an LLM
moves from `ocr` to `llm` mid-run. Scale one kind with
`docker compose up -d --scale worker-ocr=3`.

A worker is healthy when it answers `celery inspect ping` (the Compose healthcheck). `stop_grace_period`
is 30 s: a warm shutdown lets running nodes finish, and runs still going after that are failed when the
worker comes back (its start-up recovery marks the executions it owned, by node name
`<service>@<container id>`, as failed).

## The tasks

| Task | Sent by | Queue |
| --- | --- | --- |
| `flowforge.run_execution` | the API, triggers, the Telegram and Discord services | the queue of the run's next node |
| `flowforge.triggers_tick`, `flowforge.poll_email_trigger` | `beat`, every minute | `default` |
| `flowforge.ingest_document` | knowledge-base uploads | `ocr` |
| `flowforge.refine_resume` | `POST /api/resume-refinements` | `llm` |

Tasks are acknowledged late and a worker takes one at a time (`worker_prefetch_multiplier=1`), so a
worker that dies loses nothing; a re-delivered run is harmless because it claims its execution with a
compare-and-set per segment. Time limits: a run enforces `EXECUTION_TIME_LIMIT_SECONDS` (1800) itself,
with Celery's hard limits 30 and 60 s above it.

## Other services built from the API image

These are not Celery workers, but they run the same code and settings (`app/…`):

| Service | Command | What it does | How many |
| --- | --- | --- | --- |
| `beat` | `celery … beat` | sends `triggers_tick` every minute | exactly one |
| `telegram-listener` | `python -m app.telegram_listener` | long-polls the Telegram bot: the Command Center, and the Yes/No taps for Discord recordings | exactly one per bot (Telegram allows one `getUpdates` consumer) |
| `discord-bot` | `python -m app.discord_bot` | the Discord voice controller: asks in Telegram, tells the recorder to start, sends the recording | exactly one |
| `flower` | `celery … flower` | the monitoring dashboard, <http://localhost:5555> (basic auth `FLOWER_USER` / `FLOWER_PASSWORD`) | one |

`telegram-listener` also mounts the `whisper_models` volume (it transcribes voice notes itself).

`discord-recorder` is a small Node service ([`apps/discord-recorder`](../discord-recorder), discord.js),
not part of this Python image: it joins the voice channel when told to and records. It talks to
`discord-bot` through two Redis lists.

Without a token (or Discord/Telegram ids) the bot services idle instead of failing.

## Watching them

- **Flower**: workers, queues, active/reserved tasks, task history and arguments.
- `docker compose ps` shows each worker's health; `docker compose logs -f worker-llm` follows one.
- The API's `/health` also checks the database and Redis.

See the root README: "Asynchronous execution" (the run lifecycle and reliability rules) and "Workers:
queues and scaling".
