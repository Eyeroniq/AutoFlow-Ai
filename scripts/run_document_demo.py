"""Run the seeded document pipeline through the queued (Celery) path and show where each
node ran. Uses only the standard library.

    python scripts/run_document_demo.py [--api http://localhost:8000] [--file path/to/scan.pdf]

Logs in as the demo user, optionally uploads a file (else the pipeline's default, the
sample scan), queues a run, waits for it, and prints each node's queue, worker, and
duration, then the summary and entities.
"""

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PIPELINE = "Demo: Scanned invoice to entities"


def call(api, method, path, token=None, body=None, raw=None, content_type="application/json"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if raw is not None:
        data, headers["Content-Type"] = raw, content_type
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    request = urllib.request.Request(api + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path} -> {exc.code}: {exc.read().decode(errors='replace')}")
    return json.loads(text) if text else None


def upload(api, token, path: Path):
    boundary = uuid.uuid4().hex
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {kind}\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return call(api, "POST", "/api/files", token, raw=body, content_type=f"multipart/form-data; boundary={boundary}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--email", default="demo@flowforge.ai")
    parser.add_argument("--password", default="demo1234")
    parser.add_argument("--file", type=Path, help="upload and use this file instead of the sample scan")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    token = call(args.api, "POST", "/api/auth/login", body={"email": args.email, "password": args.password})["access_token"]
    workflow = next((w for w in call(args.api, "GET", "/api/workflows", token) if w["name"] == PIPELINE), None)
    if workflow is None:
        sys.exit(f"'{PIPELINE}' isn't there: run the seed first (docker compose exec api python -m app.db.seed)")
    inputs = {}
    if args.file:
        uploaded = upload(args.api, token, args.file)
        print(f"uploaded {uploaded['filename']} ({uploaded['content_type']}, {uploaded['size_bytes']} bytes): {uploaded['id']}")
        inputs = {"document": uploaded["id"]}

    accepted = call(args.api, "POST", f"/api/workflows/{workflow['id']}/run", token, body={"inputs": inputs})
    execution_id = accepted["execution_id"]
    print(f"queued {execution_id} on '{accepted['queue']}'")
    start = time.monotonic()
    while True:
        execution = call(args.api, "GET", f"/api/executions/{execution_id}", token)
        if execution["status"] in {"success", "failed", "stopped"}:
            break
        time.sleep(0.5)
    print(f"{execution['status']} in {time.monotonic() - start:.1f}s ({execution['segment']} hand-off(s))\n")
    print(f"{'node':<11}{'status':<9}{'queue':<9}{'worker':<30}duration")
    for node in execution["node_executions"]:
        duration = f"{node['duration_ms'] / 1000:.2f}s" if node["duration_ms"] is not None else "-"
        print(f"{node['node_key']:<11}{node['status']:<9}{node['queue'] or '-':<9}{node['worker_hostname'] or '-':<30}{duration}")
    if execution["status"] != "success":
        sys.exit(f"\nerror: {execution['error_message']}")
    result = execution["final_output"]["result"]
    print(f"\npages: {result['pages']}, OCR confidence: {result['ocr_confidence']}")
    print(f"\nsummary:\n{result['summary']}\n\nentities:\n{json.dumps(result['entities'], indent=2, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
