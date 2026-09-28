# FlowForge AI — Worker

Placeholder. A later phase adds the Celery worker here: it will consume workflow runs from
Redis (already provisioned in `infrastructure/docker-compose.yml`) and write
`WorkflowExecution` / `NodeExecution` rows using the models in `apps/api/app/models`.

Nothing in this directory runs yet.
