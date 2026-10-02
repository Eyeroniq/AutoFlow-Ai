"""packages/shared: the committed OpenAPI document (and so the TypeScript generated from it) matches the API."""

import json
from pathlib import Path

import pytest

from app.main import app

HERE = Path(__file__).resolve()
CANDIDATES = [parent / "packages" / "shared" / "openapi.json" for parent in (*HERE.parents, Path("/"))]


def committed() -> dict:
    for path in CANDIDATES:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    pytest.skip("packages/shared isn't available here")


def test_shared_openapi_is_current():
    """Run scripts/generate-shared-types.sh when this fails: an API change must reach the web app's types."""
    assert committed() == json.loads(json.dumps(app.openapi())), (
        "packages/shared/openapi.json is stale; run scripts/generate-shared-types.sh and commit the result"
    )


def test_response_models_are_fully_required_but_request_models_keep_optional_fields():
    schemas = app.openapi()["components"]["schemas"]
    read = schemas["WorkflowRead"]
    assert set(read["properties"]) == set(read["required"])  # served as a response only
    graph = schemas["WorkflowGraph"]
    assert "nodes" not in graph.get("required", [])  # also a request body: a client may omit it


