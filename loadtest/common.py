"""Setup shared by the Locust file and the throughput script: a dedicated load-test account
and three pipelines, created (or reused) through the real API. Standard library only.

- "Load test: OCR"  Input(File) -> OCR -> Output, entirely on the ocr queue.
- "Load test: LLM"  Input(Text) -> Groq (tiny prompt) -> Output, on the llm queue.
- "Load test: no-op" Text -> Output: portable nodes only, so a run costs one short task on
  worker-default. The API users post these, measuring POST /run without adding heavy work.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

API = os.environ.get("LOADTEST_API", "http://localhost:8000")
EMAIL = os.environ.get("LOADTEST_EMAIL", "loadtest@example.com")
PASSWORD = os.environ.get("LOADTEST_PASSWORD", "loadtest-password-1")
SAMPLE = Path(os.environ.get("LOADTEST_SAMPLE", Path(__file__).resolve().parent.parent / "samples" / "scanned-invoice.pdf"))
LLM_PROVIDER = os.environ.get("LOADTEST_LLM_PROVIDER", "groq")

OCR_PIPELINE, LLM_PIPELINE, NOOP_PIPELINE = "Load test: OCR", "Load test: LLM", "Load test: no-op"


class ApiError(Exception):
    pass


def call(method: str, path: str, token: str | None = None, body: Any = None, *, raw: bytes | None = None,
         content_type: str = "application/json", api: str = API) -> Any:
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
        raise ApiError(f"{method} {path} -> {exc.code}: {exc.read().decode(errors='replace')[:300]}") from None
    return json.loads(text) if text else None


def login(api: str = API) -> str:
    """The load-test account's access token (registered on first use)."""
    try:
        return call("POST", "/api/auth/login", body={"email": EMAIL, "password": PASSWORD}, api=api)["access_token"]
    except ApiError as exc:
        if "-> 401" not in str(exc):
            raise
    return call("POST", "/api/auth/register", api=api,
                body={"email": EMAIL, "password": PASSWORD, "full_name": "Load Test"})["access_token"]


def sample_file(token: str, api: str = API) -> str:
    """The sample scan's id among the account's uploads (uploaded once)."""
    for record in call("GET", "/api/files", token, api=api):
        if record["filename"] == SAMPLE.name:
            return record["id"]
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{SAMPLE.name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + SAMPLE.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return call("POST", "/api/files", token, raw=body, content_type=f"multipart/form-data; boundary={boundary}", api=api)["id"]


def graphs(file_id: str) -> dict[str, dict[str, Any]]:
    return {
        OCR_PIPELINE: {
            "nodes": [
                {"id": "input", "type": "input", "config": {"name": "document", "input_type": "file", "default": file_id}},
                {"id": "ocr", "type": "ocr", "config": {"file": "{{input.document}}", "dpi": 300}},
                {"id": "out", "type": "output", "config": {"value": {"pages": "{{ocr.page_count}}", "confidence": "{{ocr.mean_confidence}}"}}},
            ],
            "edges": [{"source": "input", "target": "ocr"}, {"source": "ocr", "target": "out"}],
        },
        LLM_PIPELINE: {
            "nodes": [
                {"id": "input", "type": "input", "config": {"name": "topic", "default": "load testing"}},
                {"id": "llm", "type": LLM_PROVIDER, "config": {
                    "provider": LLM_PROVIDER, "user_prompt": "Reply with the single word OK. ({{input.topic}})",
                    "max_tokens": 64, "temperature": 0,
                }},
                {"id": "out", "type": "output", "config": {"value": "{{llm.response}}"}},
            ],
            "edges": [{"source": "input", "target": "llm"}, {"source": "llm", "target": "out"}],
        },
        NOOP_PIPELINE: {
            "nodes": [
                {"id": "text", "type": "text", "config": {"text": "hello"}},
                {"id": "out", "type": "output", "config": {"value": "{{text.text}}"}},
            ],
            "edges": [{"source": "text", "target": "out"}],
        },
    }


def ensure_pipelines(token: str, api: str = API) -> dict[str, str]:
    """{name: workflow id}, creating or updating the load-test pipelines."""
    existing = {w["name"]: w["id"] for w in call("GET", "/api/workflows", token, api=api)}
    ids = {}
    for name, graph in graphs(sample_file(token, api)).items():
        workflow_id = existing.get(name) or call("POST", "/api/workflows", token, body={"name": name}, api=api)["id"]
        call("PUT", f"/api/workflows/{workflow_id}", token, body={"graph": graph}, api=api)
        ids[name] = workflow_id
    return ids
