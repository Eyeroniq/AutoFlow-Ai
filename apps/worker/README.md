# FlowForge AI — Worker

The Celery worker's code lives in the API package, [`apps/api/app/worker`](../api/app/worker),
because it uses the same models, settings, credential resolution, and run service as the
API (`app.services.runs.run_execution`). The Compose `worker` service runs it from the API
image:

```bash
celery -A app.worker.celery_app:celery_app worker --hostname=worker@%h --queues=default --concurrency=4
```

See the root README: "Asynchronous execution" (lifecycle and reliability rules) and
"Workers: running and scaling".
