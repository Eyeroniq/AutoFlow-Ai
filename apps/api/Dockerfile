FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Tesseract for the OCR node, with the language data the node's `language` option can use
# (tesseract-ocr pulls in English). The API and every worker share this image, so OCR
# also works for "Test node" and ?sync=true runs. Add languages with
#   docker compose build --build-arg TESSERACT_LANGS="deu fra hin" api
ARG TESSERACT_LANGS="deu fra spa ita por"
# ffmpeg (with ffprobe) for Speech to Text: probing, extracting audio from video,
# converting, and splitting long recordings into chunks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-osd ffmpeg \
       $(for lang in $TESSERACT_LANGS; do printf 'tesseract-ocr-%s ' "$lang"; done) \
    && rm -rf /var/lib/apt/lists/*

# The workflow engine lives in packages/, outside this build context; docker-compose
# passes it in as the named context "workflow_engine". requirements.txt installs it
# (editable) from ../../packages/workflow-engine, i.e. /packages/workflow-engine.
COPY --from=workflow_engine . /packages/workflow-engine

# Dev image: includes the test dependencies so `docker compose exec api pytest` works.
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY . .

# /data/files is the upload volume shared by the API and the workers; /data/models holds
# the faster-whisper models, downloaded on first use.
RUN useradd --create-home --uid 1000 appuser && mkdir -p /data/files /data/models \
    && chown appuser:appuser /data/files /data/models
USER appuser

EXPOSE 8000

# docker-compose overrides this with a migrate-then-reload dev command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
