"""API latency while heavy runs are in flight.

Two kinds of simulated users, against the real API:

- ApiUser (the rest of --users): the traffic being measured. Lists workflows
  (GET /api/workflows, 3 of 4 requests) and queues runs (POST /api/workflows/{id}/run,
  1 of 4) of the "no-op" pipeline, which adds almost no work.
- RunDriver (exactly LOADTEST_INFLIGHT users): each keeps one heavy run in flight
  (LOADTEST_MIX=ocr or llm): it queues a run, polls it until it finishes, and repeats. So
  there are always N OCR (or LLM) runs queued or running, and their end-to-end time is
  reported as request type RUN.

    docker compose --profile loadtest run --rm locust --headless -u 25 -r 10 -t 2m \\
        --csv /mnt/loadtest/results/high  # with LOADTEST_INFLIGHT=12 in the environment
"""

from __future__ import annotations

import os
import time

from locust import HttpUser, between, constant, events, task

from common import LLM_PIPELINE, NOOP_PIPELINE, OCR_PIPELINE, ensure_pipelines, login

INFLIGHT = int(os.environ.get("LOADTEST_INFLIGHT", "2"))
MIX = os.environ.get("LOADTEST_MIX", "ocr")
POLL_SECONDS = float(os.environ.get("LOADTEST_POLL_SECONDS", "0.5"))
TERMINAL = {"success", "failed", "stopped"}

setup: dict[str, object] = {}


@events.test_start.add_listener
def _setup(environment, **_):
    # One login for everyone: the auth endpoints are rate limited (AUTH_RATE_LIMIT).
    token = login(api=environment.host)
    setup["token"] = token
    setup["ids"] = ensure_pipelines(token, api=environment.host)
    print(f"load test: {INFLIGHT} {MIX} run(s) in flight; pipelines {setup['ids']}")


class _Authed(HttpUser):
    abstract = True

    def on_start(self):
        self.client.headers["Authorization"] = f"Bearer {setup['token']}"


class ApiUser(_Authed):
    wait_time = between(0.5, 1.5)

    @task(3)
    def list_workflows(self):
        self.client.get("/api/workflows", name="GET /api/workflows")

    @task(1)
    def queue_run(self):
        workflow_id = setup["ids"][NOOP_PIPELINE]
        with self.client.post(f"/api/workflows/{workflow_id}/run", json={"inputs": {}},
                              name="POST /api/workflows/[id]/run", catch_response=True) as response:
            if response.status_code != 202:
                response.failure(f"expected 202, got {response.status_code}: {response.text[:200]}")


class RunDriver(_Authed):
    fixed_count = INFLIGHT
    wait_time = constant(0)

    @task
    def one_run(self):
        workflow_id = setup["ids"][OCR_PIPELINE if MIX == "ocr" else LLM_PIPELINE]
        start = time.perf_counter()
        with self.client.post(f"/api/workflows/{workflow_id}/run", json={"inputs": {}},
                              name=f"POST run ({MIX}, driver)", catch_response=True) as response:
            if response.status_code != 202:
                response.failure(f"expected 202, got {response.status_code}")
                time.sleep(1)
                return
            execution_id = response.json()["execution_id"]
        status, error = None, None
        while status not in TERMINAL:
            time.sleep(POLL_SECONDS)
            poll = self.client.get(f"/api/executions/{execution_id}", name="GET /api/executions/[id] (driver poll)")
            if poll.ok:
                body = poll.json()
                status, error = body["status"], body["error_message"]
        events.request.fire(
            request_type="RUN",
            name=f"{MIX} run, queued to finished",
            response_time=(time.perf_counter() - start) * 1000,
            response_length=0,
            exception=None if status == "success" else RuntimeError(f"{status}: {error}"),
            context={},
        )
