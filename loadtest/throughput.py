"""Run throughput: queue K runs at once and time how long the workers take to drain them.

    python loadtest/throughput.py --runs 36 --label "worker-ocr x1"

Standard library only; talks to the real API as the load-test account. Reports runs per
minute over the whole batch (first POST to last finish), and each run's queue wait
(queued -> a worker started it) and duration (started -> finished), from the API's own
timestamps. Scale the workers between batches, e.g.

    docker compose up -d --no-recreate --scale worker-ocr=3
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import API, LLM_PIPELINE, OCR_PIPELINE, call, ensure_pipelines, login  # noqa: E402

TERMINAL = {"success", "failed", "stopped"}


def ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=36)
    parser.add_argument("--mix", choices=["ocr", "llm"], default="ocr")
    parser.add_argument("--label", default="")
    parser.add_argument("--api", default=API)
    parser.add_argument("--out", type=Path, help="append the result as a JSON line to this file")
    args = parser.parse_args()

    token = login(api=args.api)
    workflow_id = ensure_pipelines(token, api=args.api)[OCR_PIPELINE if args.mix == "ocr" else LLM_PIPELINE]

    start = time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(
            lambda _: call("POST", f"/api/workflows/{workflow_id}/run", token, body={"inputs": {}}, api=args.api)["execution_id"],
            range(args.runs),
        ))
    submitted = time.time() - start
    print(f"queued {len(ids)} {args.mix} runs in {submitted:.1f}s; waiting...", flush=True)

    pending = set(ids)
    rows: dict[str, dict] = {}
    while pending:
        time.sleep(1)
        for execution_id in list(pending):
            row = call("GET", f"/api/executions/{execution_id}", token, api=args.api)
            if row["status"] in TERMINAL:
                rows[execution_id] = row
                pending.discard(execution_id)
    wall = time.time() - start

    finished = [ts(r["finished_at"]) for r in rows.values()]
    created = [ts(r["created_at"]) for r in rows.values()]
    batch_seconds = (max(finished) - min(created)).total_seconds()
    waits = [(ts(r["started_at"]) - ts(r["created_at"])).total_seconds() for r in rows.values() if r["started_at"]]
    durations = [r["duration_ms"] / 1000 for r in rows.values() if r["duration_ms"] is not None]
    workers = sorted({n["worker_hostname"] for r in rows.values() for n in r["node_executions"] if n["worker_hostname"]})
    result = {
        "label": args.label,
        "mix": args.mix,
        "runs": len(rows),
        "succeeded": sum(r["status"] == "success" for r in rows.values()),
        "failed": sum(r["status"] != "success" for r in rows.values()),
        "batch_seconds": round(batch_seconds, 1),
        "runs_per_minute": round(len(rows) / batch_seconds * 60, 1),
        "queue_wait_p50_s": round(statistics.median(waits), 1),
        "queue_wait_p95_s": round(pct(waits, 95), 1),
        "run_seconds_p50": round(statistics.median(durations), 2),
        "run_seconds_p95": round(pct(durations, 95), 2),
        "workers": workers,
        "wall_seconds": round(wall, 1),
    }
    errors = sorted({r["error_message"] for r in rows.values() if r["status"] != "success"})
    if errors:
        result["errors"] = errors[:5]
    print(json.dumps(result, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
