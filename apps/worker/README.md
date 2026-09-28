# FlowForge AI — Workers

The Celery worker's code lives in the API package, [`apps/api/app/worker`](../api/app/worker),
because it uses the same models, settings, credential resolution, and run service as the
API (`app.services.runs.run_execution`). Compose runs it from the API image as three
services, one per queue, so each kind of work scales on its own:

| Service          | Queue     | Runs                                  | Concurrency (env)                     |
| ---------------- | --------- | ------------------------------------- | ------------------------------------- |
| `worker-default` | `default` | HTTP Request, Gmail, Gmail Read, Delay | `WORKER_DEFAULT_CONCURRENCY` (4)     |
| `worker-llm`     | `llm`     | LLM nodes, Summarize, Entity Extraction | `WORKER_LLM_CONCURRENCY` (8)       |
| `worker-ocr`     | `ocr`     | PDF Extract, OCR                      | `WORKER_OCR_CONCURRENCY` (2)          |

Each one is

```bash
celery -A app.worker.celery_app:celery_app worker --hostname=worker-ocr@%h --queues=ocr --concurrency=2
```

and a run hands itself from queue to queue as it reaches each kind of node. Scale one kind
with `docker compose up -d --scale worker-ocr=3`.

See the root README: "Asynchronous execution" (lifecycle and reliability rules) and
"Workers: queues and scaling".
