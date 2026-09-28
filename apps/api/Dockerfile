FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The workflow engine lives in packages/, outside this build context; docker-compose
# passes it in as the named context "workflow_engine". requirements.txt installs it
# (editable) from ../../packages/workflow-engine, i.e. /packages/workflow-engine.
COPY --from=workflow_engine . /packages/workflow-engine

# Dev image: includes the test dependencies so `docker compose exec api pytest` works.
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY . .

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# docker-compose overrides this with a migrate-then-reload dev command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
