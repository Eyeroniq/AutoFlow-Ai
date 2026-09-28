"""POST /api/files and friends: type detection, size limits, safe names, owner-only access,
and file inputs on runs."""

import io

import pymupdf
import pytest

from app.core.config import settings
from app.models.execution import WorkflowExecution
from app.services.files import safe_filename, sniff_content_type
from app.services.runs import run_execution
from tests.support import create_workflow, queue_run


def pdf_bytes(text: str = "Hello from a PDF with a proper text layer.") -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), text, fontsize=12)
    return doc.tobytes()


PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def upload(client, user, content: bytes, filename: str = "doc.pdf", content_type: str = "application/pdf"):
    return await client.post("/api/files", files={"file": (filename, io.BytesIO(content), content_type)}, headers=user.headers)


async def test_upload_stores_the_file_and_detects_its_type(client, user, files_dir):
    content = pdf_bytes()
    response = await upload(client, user, content, content_type="image/png")  # the client's type is ignored
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["content_type"] == "application/pdf" and body["filename"] == "doc.pdf"
    assert body["size_bytes"] == len(content) and len(body["sha256"]) == 64
    # Stored under <owner>/<file id>, never under the uploaded name.
    stored = files_dir / user.id / body["id"]
    assert stored.read_bytes() == content

    download = await client.get(f"/api/files/{body['id']}/content", headers=user.headers)
    assert download.status_code == 200 and download.content == content
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["x-content-type-options"] == "nosniff"

    listing = (await client.get("/api/files", headers=user.headers)).json()
    assert [f["id"] for f in listing] == [body["id"]]


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"%PDF-1.7\n", "application/pdf"),
        (PNG_HEAD, "image/png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "image/jpeg"),
        (b"II*\x00" + b"\x00" * 20, "image/tiff"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"GIF89a" + b"\x00" * 10, "image/gif"),
        (b"BM" + b"\x00" * 12 + (40).to_bytes(4, "little") + b"\x00" * 8, "image/bmp"),
        ("Plain text, with ünïcode.".encode(), "text/plain"),
        (b"PK\x03\x04zipped", None),  # zip / docx
        (b"MZ\x90\x00\x03\x00", None),  # Windows executable
        (b"\x7fELF\x02\x01\x01\x00", None),
        (b"BMW is a word, not a bitmap", "text/plain"),
    ],
)
def test_types_come_from_the_content(head, expected):
    assert sniff_content_type(head) == expected


async def test_disallowed_and_empty_files_are_refused(client, user, files_dir):
    zipped = await upload(client, user, b"PK\x03\x04" + b"\x00" * 100, filename="evil.pdf")
    assert zipped.status_code == 415 and "Unsupported file type" in zipped.json()["detail"]
    empty = await upload(client, user, b"", filename="empty.pdf")
    assert empty.status_code == 400 and empty.json()["detail"] == "The file is empty"
    missing = await client.post("/api/files", files={"other": ("x.pdf", io.BytesIO(b"%PDF-"), "application/pdf")},
                                headers=user.headers)
    assert missing.status_code == 400 and "multipart field 'file'" in missing.json()["detail"]
    # Nothing (not even a partial file) was left behind.
    assert not any(p.is_file() for p in files_dir.rglob("*"))


async def test_the_size_limit(client, user, monkeypatch, files_dir):
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0.1)  # 104,857 bytes
    too_big = await upload(client, user, b"%PDF-" + b"0" * 300_000)
    assert too_big.status_code == 413 and too_big.json()["detail"] == "The file is larger than the 0.1 MB limit"
    # A body that claims to be small but isn't is caught while streaming.
    just_over = await upload(client, user, b"%PDF-" + b"0" * 110_000)
    assert just_over.status_code == 413
    ok = await upload(client, user, b"%PDF-" + b"0" * 90_000)
    assert ok.status_code == 201
    assert len([p for p in files_dir.rglob("*") if p.is_file()]) == 1

    async def chunks():
        yield b"--x\r\n"

    no_length = await client.post("/api/files", content=chunks(), headers={
        **user.headers, "Content-Type": "multipart/form-data; boundary=x"})
    assert no_length.status_code == 411


def test_safe_filenames():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename(r"C:\Users\me\scan 01.pdf") == "scan 01.pdf"
    assert safe_filename("in\x00vo\x1bice\n.pdf") == "invoice.pdf"
    assert safe_filename("   ...  ") == "upload"
    assert safe_filename(None) == "upload"
    long = safe_filename("a" * 300 + ".pdf")
    assert len(long) == 200 and long.endswith(".pdf")


async def test_path_tricks_in_the_name_stay_inside_the_owners_folder(client, user, files_dir):
    response = await upload(client, user, pdf_bytes(), filename="../../../outside.pdf")
    assert response.status_code == 201 and response.json()["filename"] == "outside.pdf"
    files = [p for p in files_dir.rglob("*") if p.is_file()]
    assert files == [files_dir / user.id / response.json()["id"]]


async def test_only_the_owner_can_see_or_delete_a_file(client, user, user_factory, files_dir):
    file_id = (await upload(client, user, pdf_bytes())).json()["id"]
    stranger = await user_factory()
    for method, path in (("GET", f"/api/files/{file_id}"), ("GET", f"/api/files/{file_id}/content"),
                         ("DELETE", f"/api/files/{file_id}")):
        response = await client.request(method, path, headers=stranger.headers)
        assert response.status_code == 404, (method, path)
    assert (await client.get("/api/files", headers=stranger.headers)).json() == []
    assert (await client.get(f"/api/files/{file_id}")).status_code == 401

    assert (await client.delete(f"/api/files/{file_id}", headers=user.headers)).status_code == 204
    assert not (files_dir / user.id / file_id).exists()
    assert (await client.get(f"/api/files/{file_id}", headers=user.headers)).status_code == 404


def document_graph():
    return {
        "nodes": [
            {"id": "input", "type": "input", "config": {"name": "document", "input_type": "file"}},
            {"id": "pdf", "type": "pdf_extract", "config": {"file": "{{input.document}}"}},
            {"id": "out", "type": "output", "config": {"value": "{{pdf.text}}"}},
        ],
        "edges": [{"source": "input", "target": "pdf"}, {"source": "pdf", "target": "out"}],
    }


async def test_runs_only_accept_the_callers_own_files(client, user, user_factory, task_queue):
    stranger = await user_factory()
    theirs = (await upload(client, stranger, pdf_bytes())).json()["id"]
    wid = await create_workflow(client, user, document_graph())

    refused = await client.post(f"/api/workflows/{wid}/run", json={"inputs": {"document": theirs}}, headers=user.headers)
    assert refused.status_code == 422
    issue = refused.json()["detail"]["errors"][0]
    assert issue["code"] == "invalid_input" and issue["node_id"] == "input"
    assert "the file was not found (it doesn't exist or isn't yours)" in issue["message"]
    garbage = await client.post(f"/api/workflows/{wid}/run", json={"inputs": {"document": "not-an-id"}},
                                headers=user.headers)
    assert "is not a file id" in garbage.json()["detail"]["errors"][0]["message"]
    assert task_queue.enqueued == []  # nothing was queued


async def test_a_file_input_runs_through_pdf_extract(client, user, task_queue, session_factory, redis, shared_session):
    mine = (await upload(client, user, pdf_bytes("Quarterly report: revenue grew 12 percent."))).json()["id"]
    wid = await create_workflow(client, user, document_graph())
    execution_id = await queue_run(client, user, wid, {"document": mine})
    assert task_queue.enqueued == [(execution_id, "ocr")]  # PDF Extract is the first real node

    summary = await run_execution(execution_id, session_factory=session_factory, redis=redis, worker_id="w@test")
    assert summary["status"] == "success", summary
    detail = (await client.get(f"/api/executions/{execution_id}", headers=user.headers)).json()
    assert detail["final_output"] == {"result": "Quarterly report: revenue grew 12 percent."}
    by_key = {n["node_key"]: n for n in detail["node_executions"]}
    assert by_key["input"]["output"]["document"]["file_id"] == mine
    assert by_key["input"]["output"]["document"]["filename"] == "doc.pdf"
    row = await shared_session.get(WorkflowExecution, execution_id)
    assert row.inputs_json == {"document": mine}
