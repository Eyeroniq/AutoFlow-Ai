# Load tests

API latency while heavy runs are in flight, and run throughput as `worker-ocr` scales. The
measured results and the exact conditions are in the root README ("Load test"); the raw
output of each suite run is kept under `results/<UTC timestamp>/`.

## What runs

- [`locustfile.py`](locustfile.py) (Locust, from the official `locustio/locust` image on the
  Compose network, so it calls `http://api:8000` directly):
  - `ApiUser`: the measured traffic. Every 0.5–1.5 s it either lists workflows
    (`GET /api/workflows`, 3 in 4) or queues a run (`POST /api/workflows/{id}/run`, 1 in 4)
    of a no-op pipeline, which costs one short task on `worker-default`.
  - `RunDriver` (`LOADTEST_INFLIGHT` of them): each keeps one heavy run queued or running
    at all times (queue it, poll `GET /api/executions/{id}` every 0.5 s until it finishes,
    repeat). `LOADTEST_MIX=ocr` uses Input(File) → OCR → Output on the two-page sample scan at
    300 dpi; `llm` uses Input → one short LLM call (`LOADTEST_LLM_PROVIDER`, default `groq`)
    → Output. Their end-to-end time is reported as request type `RUN`.
- [`throughput.py`](throughput.py) (standard library): queues N runs at once, waits for all of
  them, and reports runs per minute plus each run's queue wait and duration, from the API's
  timestamps.
- [`common.py`](common.py): a dedicated account (`loadtest@example.com`, registered on first
  use, so the demo account's history stays clean) and the three pipelines, created through
  the API.
- [`run_suite.sh`](run_suite.sh): everything, in order, recording the machine and settings.

## Run it

```bash
docker compose up -d                  # the stack, with the three workers
bash loadtest/run_suite.sh            # about 15 minutes; results in loadtest/results/<stamp>/
```

One scenario by hand (on Windows' Git Bash, set `MSYS_NO_PATHCONV=1` so `/mnt/...` isn't
rewritten into a Windows path):

```bash
LOADTEST_INFLIGHT=12 docker compose --profile loadtest run --rm locust \
  --headless -u 62 -r 10 -t 2m --csv /mnt/loadtest/results/manual/high
python loadtest/throughput.py --runs 36 --label "worker-ocr x3"
```

`-u` is the total number of users: `LOADTEST_INFLIGHT` drivers plus API users. Without
`--headless`, Locust's web UI is at http://localhost:8089.

Keep LLM runs short and few: free tiers have per-minute limits (Groq's is 30 requests per
minute for `openai/gpt-oss-20b`), and retries after a `429` make the runs, not the API,
the thing being measured. The OCR mix involves no external service.

To remove the load-test data afterwards, delete the account's "Load test: ..." pipelines
(their executions go with them), e.g. from the dashboard after signing in as
`loadtest@example.com` / `loadtest-password-1`.
