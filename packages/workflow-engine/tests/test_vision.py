"""Vision: the image and prompt sent to Gemini, conversion of types Gemini doesn't take,
optional schema validation with one retry, errors, and auth-missing validation."""

import io

import pytest
from PIL import Image

from flowforge_engine import (
    ExecutionServices,
    IssueCode,
    LocalFileStore,
    NodeStatus,
    ProviderSettings,
    WorkflowGraph,
    execute_node,
    validate_workflow,
)
from flowforge_engine.errors import ProviderError
from flowforge_engine.testing import make_context, node

RECEIPT_SCHEMA = {
    "type": "object", "required": ["total", "items"], "additionalProperties": False,
    "properties": {"total": {"type": "number"}, "items": {"type": "array", "items": {"type": "string"}}},
}


class FakeGemini:
    """Records each vision call and answers from a script."""

    is_mock = False
    name = "gemini"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def generate_with_image(self, system_prompt, user_prompt, image, mime_type, model, temperature, max_tokens):
        self.calls.append({"system": system_prompt, "prompt": user_prompt, "image": image, "mime": mime_type, "model": model,
                           "temperature": temperature, "max_tokens": max_tokens})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def image_file(tmp_path, fmt="PNG", name="photo.png"):
    path = tmp_path / name
    Image.new("RGB", (8, 6), (200, 30, 30)).save(path, format=fmt)
    return path


def setup(tmp_path, gemini, fmt="PNG", content_type="image/png", name="photo.png"):
    store = LocalFileStore()
    stored = store.add(image_file(tmp_path, fmt, name), content_type=content_type)
    services = ExecutionServices(provider_settings=ProviderSettings(testing=True), files=store, llm_providers={"gemini": gemini})
    return stored, make_context(services=services)


async def test_describes_an_image(tmp_path):
    gemini = FakeGemini("A red rectangle.")
    stored, context = setup(tmp_path, gemini)
    result = await execute_node(node("v", "vision", image=stored.id, prompt="What is this?", model="gemini-x"), context)
    assert result.status is NodeStatus.SUCCESS, result.error
    assert result.output["text"] == "A red rectangle." and result.output["data"] is None
    assert result.output["converted"] is False and result.output["model"] == "gemini-x" and result.output["attempts"] == 1
    [call] = gemini.calls
    assert call["mime"] == "image/png" and call["image"] == stored.path.read_bytes()
    assert call["prompt"] == "What is this?" and call["system"] == ""


async def test_bmp_is_sent_as_png(tmp_path):
    gemini = FakeGemini("ok")
    stored, context = setup(tmp_path, gemini, fmt="BMP", content_type="image/bmp", name="scan.bmp")
    result = await execute_node(node("v", "vision", image=stored.id, prompt="Describe"), context)
    assert result.output["converted"] is True and result.output["content_type"] == "image/bmp"
    call = gemini.calls[0]
    assert call["mime"] == "image/png"
    assert Image.open(io.BytesIO(call["image"])).size == (8, 6)


async def test_schema_reply_is_validated_and_retried_once(tmp_path):
    gemini = FakeGemini('{"total": "12.50"}', '```json\n{"total": 12.5, "items": ["coffee", "bagel"]}\n```')
    stored, context = setup(tmp_path, gemini)
    result = await execute_node(node("v", "vision", image=stored.id, prompt="Read the receipt", schema=RECEIPT_SCHEMA), context)
    assert result.status is NodeStatus.SUCCESS, result.error
    assert result.output["data"] == {"total": 12.5, "items": ["coffee", "bagel"]} and result.output["attempts"] == 2
    first, second = gemini.calls
    assert '"required": ["total", "items"]' in first["prompt"] and first["system"] == "You reply with JSON only."
    assert "didn't match the schema" in second["prompt"] and "items" in second["prompt"]


async def test_schema_failure_after_two_attempts(tmp_path):
    gemini = FakeGemini("not json", '{"total": 1}')
    stored, context = setup(tmp_path, gemini)
    result = await execute_node(node("v", "vision", image=stored.id, prompt="Read", schema=RECEIPT_SCHEMA), context)
    assert result.status is NodeStatus.FAILED and "after 2 attempts" in result.error and "items" in result.error


async def test_provider_errors_and_wrong_files_fail_the_node(tmp_path):
    gemini = FakeGemini(ProviderError("gemini", "rate limited (HTTP 429)"))
    stored, context = setup(tmp_path, gemini)
    result = await execute_node(node("v", "vision", image=stored.id, prompt="x"), context)
    assert result.status is NodeStatus.FAILED and "HTTP 429" in result.error

    store = LocalFileStore()
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    doc = store.add(pdf, content_type="application/pdf")
    context = make_context(services=ExecutionServices(provider_settings=ProviderSettings(testing=True), files=store,
                                                      llm_providers={"gemini": FakeGemini()}))
    result = await execute_node(node("v", "vision", image=doc.id, prompt="x"), context)
    assert result.status is NodeStatus.FAILED and "this node reads image/" in result.error


def test_bad_schema_and_auth_missing_are_validation_errors():
    graph = WorkflowGraph.model_validate({"nodes": [
        {"id": "v", "type": "vision", "config": {"image": "f1", "prompt": "x"}},
        {"id": "w", "type": "vision", "config": {"image": "f1", "prompt": "x", "schema": {"type": "banana"}}},
    ], "edges": []})
    issues = validate_workflow(graph, services=ExecutionServices(provider_settings=ProviderSettings()))
    by_node = {(i.node_id, i.code) for i in issues}
    assert ("v", IssueCode.AUTH_MISSING) in by_node
    assert any(i.node_id == "w" and "banana" in i.message for i in issues)
    ok = validate_workflow(WorkflowGraph.model_validate({"nodes": [graph.nodes[0].model_dump()], "edges": []}),
                           services=ExecutionServices(provider_settings=ProviderSettings(gemini={"api_key": "g"})))
    assert ok == []


@pytest.mark.parametrize("fmt,content_type", [("GIF", "image/gif"), ("TIFF", "image/tiff")])
async def test_other_upload_types_convert(tmp_path, fmt, content_type):
    gemini = FakeGemini("ok")
    stored, context = setup(tmp_path, gemini, fmt=fmt, content_type=content_type, name=f"img.{fmt.lower()}")
    result = await execute_node(node("v", "vision", image=stored.id, prompt="x"), context)
    assert result.status is NodeStatus.SUCCESS and gemini.calls[0]["mime"] == "image/png"
