"""Knowledge bases over the API: CRUD, uploads queued for ingestion, ingestion into pgvector,
search, citations back to a chunk, owner-only access, and the nodes in a real run."""

import io
import uuid

import pymupdf
from sqlalchemy import select

from app.models.knowledge import KnowledgeChunk
from app.services.knowledge import run_ingestion
from tests.support import create_workflow

POLICY = (
    "Acme Travel Policy\n\n"
    "Hotel stays are reimbursed up to 150 USD per night in most cities, and up to 250 USD per night in Tokyo.\n\n"
    "Meal allowance is 60 USD per day while travelling.\n\n"
    "Receipts must be submitted within 30 days of returning, through the expenses portal."
)
IT = "IT handbook\n\nLaptops are replaced every three years. Printers are on the second floor."


async def make_kb(client, user, name="Policies", **settings):
    body = {"name": name, "embedding_provider": "mock", "chunk_size": 200, "chunk_overlap": 0, **settings}
    response = await client.post("/api/knowledge-bases", json=body, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def upload(client, user, kb_id, content: bytes, filename="policy.txt", content_type="text/plain"):
    return await client.post(
        f"/api/knowledge-bases/{kb_id}/documents",
        files={"file": (filename, io.BytesIO(content), content_type)}, headers=user.headers,
    )


async def add_ready(client, user, session_factory, kb_id, text, filename="policy.txt"):
    response = await upload(client, user, kb_id, text.encode(), filename)
    assert response.status_code == 202, response.text
    doc = response.json()
    result = await run_ingestion(session_factory, uuid.UUID(doc["id"]))
    assert result["status"] == "ready", result
    return doc


async def test_create_list_rename_delete(client, user):
    kb = await make_kb(client, user, description="Company rules")
    assert kb["embedding_provider"] == "mock" and kb["dimensions"] == 768 and kb["document_count"] == 0
    assert (await client.post("/api/knowledge-bases", json={"name": "Policies", "embedding_provider": "mock"},
                              headers=user.headers)).status_code == 409
    listing = (await client.get("/api/knowledge-bases", headers=user.headers)).json()
    assert [k["name"] for k in listing] == ["Policies"]
    renamed = await client.patch(f"/api/knowledge-bases/{kb['id']}", json={"name": "Rules"}, headers=user.headers)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Rules"
    assert (await client.delete(f"/api/knowledge-bases/{kb['id']}", headers=user.headers)).status_code == 204
    assert (await client.get(f"/api/knowledge-bases/{kb['id']}", headers=user.headers)).status_code == 404


async def test_settings_are_validated(client, user):
    response = await client.post(
        "/api/knowledge-bases", json={"name": "x", "embedding_provider": "mock", "chunk_size": 300, "chunk_overlap": 300},
        headers=user.headers,
    )
    assert response.status_code == 422
    braces = await client.post("/api/knowledge-bases", json={"name": "{{x}}"}, headers=user.headers)
    assert braces.status_code == 422


async def test_upload_is_queued_then_ingested_into_pgvector(client, user, session_factory, ingest_queue, shared_session):
    kb = await make_kb(client, user)
    response = await upload(client, user, kb["id"], POLICY.encode())
    assert response.status_code == 202, response.text
    doc = response.json()
    assert doc["status"] == "pending" and doc["source_type"] == "text"
    assert ingest_queue.queued == [uuid.UUID(doc["id"])]

    result = await run_ingestion(session_factory, uuid.UUID(doc["id"]))
    assert result["status"] == "ready" and result["chunks"] >= 2
    [listed] = (await client.get(f"/api/knowledge-bases/{kb['id']}/documents", headers=user.headers)).json()
    assert listed["status"] == "ready" and listed["method"] == "text" and listed["char_count"] == len(POLICY)
    chunks = (await shared_session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == uuid.UUID(doc["id"])))).all()
    assert len(chunks) == listed["chunk_count"] and all(len(c.embedding) == 768 for c in chunks)
    assert sorted(c.chunk_index for c in chunks) == list(range(len(chunks)))

    # Running the task again (a redelivered message) leaves a ready document alone.
    assert (await run_ingestion(session_factory, uuid.UUID(doc["id"])))["skipped"] is True


async def test_search_ranks_by_meaning_and_a_citation_leads_to_the_chunk(client, user, session_factory):
    kb = await make_kb(client, user)
    await add_ready(client, user, session_factory, kb["id"], POLICY, "policy.txt")
    await add_ready(client, user, session_factory, kb["id"], IT, "it.txt")
    response = await client.post(
        f"/api/knowledge-bases/{kb['id']}/search", json={"query": "hotel per night in tokyo", "top_k": 3},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert results[0]["filename"] == "policy.txt" and "Tokyo" in results[0]["content"]
    assert [r["rank"] for r in results] == [1, 2, 3]
    assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)

    chunk = await client.get(f"/api/knowledge-bases/{kb['id']}/chunks/{results[0]['chunk_id']}", headers=user.headers)
    assert chunk.status_code == 200
    assert chunk.json()["content"] == results[0]["content"] and chunk.json()["filename"] == "policy.txt"
    assert POLICY[chunk.json()["start"]:chunk.json()["end"]] == chunk.json()["content"]


async def test_pdfs_keep_page_numbers(client, user, session_factory):
    kb = await make_kb(client, user)
    pdf = pymupdf.open()
    for text in ("Page one is about onboarding and badges.", "Page two: the Tokyo hotel limit is 250 USD."):
        pdf.new_page().insert_text((72, 100), text, fontsize=12)
    response = await upload(client, user, kb["id"], pdf.tobytes(), "handbook.pdf", "application/pdf")
    assert response.json()["source_type"] == "pdf"
    assert (await run_ingestion(session_factory, uuid.UUID(response.json()["id"])))["status"] == "ready"
    results = (await client.post(f"/api/knowledge-bases/{kb['id']}/search", json={"query": "tokyo hotel limit"},
                                 headers=user.headers)).json()["results"]
    assert results[0]["page"] == 2 and results[0]["filename"] == "handbook.pdf"


async def test_unsupported_and_empty_files(client, user, session_factory):
    kb = await make_kb(client, user)
    audio = await upload(client, user, kb["id"], b"ID3" + b"\x00" * 64, "a.mp3", "audio/mpeg")
    assert audio.status_code == 415
    empty = await upload(client, user, kb["id"], b"   \n\n   ", "blank.txt")
    assert empty.status_code == 202
    result = await run_ingestion(session_factory, uuid.UUID(empty.json()["id"]))
    assert result["status"] == "failed" and "No text found" in result["error"]
    [doc] = (await client.get(f"/api/knowledge-bases/{kb['id']}/documents", headers=user.headers)).json()
    assert doc["status"] == "failed" and "No text found" in doc["error"]
    retry = await client.post(f"/api/knowledge-bases/{kb['id']}/documents/{doc['id']}/retry", headers=user.headers)
    assert retry.status_code == 202 and retry.json()["status"] == "pending"


async def test_queue_down_marks_the_document_failed(client, user, ingest_queue):
    kb = await make_kb(client, user)
    ingest_queue.fail = True
    response = await upload(client, user, kb["id"], POLICY.encode())
    assert response.status_code == 503
    [doc] = (await client.get(f"/api/knowledge-bases/{kb['id']}/documents", headers=user.headers)).json()
    assert doc["status"] == "failed" and "Couldn't queue" in doc["error"]


async def test_add_an_already_uploaded_file_and_delete_a_document(client, user, session_factory):
    kb = await make_kb(client, user)
    file_id = (await client.post("/api/files", files={"file": ("p.txt", io.BytesIO(POLICY.encode()), "text/plain")},
                                 headers=user.headers)).json()["id"]
    response = await client.post(f"/api/knowledge-bases/{kb['id']}/documents/from-file", json={"file_id": file_id},
                                 headers=user.headers)
    assert response.status_code == 202 and response.json()["file_id"] == file_id
    await run_ingestion(session_factory, uuid.UUID(response.json()["id"]))
    deleted = await client.delete(f"/api/knowledge-bases/{kb['id']}/documents/{response.json()['id']}", headers=user.headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/knowledge-bases/{kb['id']}", headers=user.headers)).json()["chunk_count"] == 0
    # The upload itself stays under Files.
    assert (await client.get(f"/api/files/{file_id}", headers=user.headers)).status_code == 200


async def test_other_users_cant_see_or_use_a_knowledge_base(client, user, user_factory, session_factory):
    kb = await make_kb(client, user)
    other = await user_factory()
    assert (await client.get(f"/api/knowledge-bases/{kb['id']}", headers=other.headers)).status_code == 404
    assert (await client.post(f"/api/knowledge-bases/{kb['id']}/search", json={"query": "x"},
                              headers=other.headers)).status_code == 404
    assert (await upload(client, other, kb["id"], b"hi")).status_code == 404
    # Their own knowledge base may have the same name.
    await make_kb(client, other)


async def test_add_document_retriever_and_reranker_in_a_run(client, user, session_factory):
    """PDF to Knowledge Base, then Document Q&A, as pipelines (mock LLM and embeddings)."""
    await make_kb(client, user, name="Handbook")
    file_id = (await client.post("/api/files", files={"file": ("policy.txt", io.BytesIO(POLICY.encode()), "text/plain")},
                                 headers=user.headers)).json()["id"]
    ingest = await create_workflow(client, user, {
        "nodes": [
            {"id": "doc", "type": "input", "config": {"name": "document", "input_type": "file"}},
            {"id": "add", "type": "kb_add_document", "config": {"knowledge_base": "Handbook", "file": "{{doc.value}}"}},
            {"id": "out", "type": "output", "config": {"value": {"status": "{{add.status}}", "chunks": "{{add.chunk_count}}"}}},
        ],
        "edges": [{"source": "doc", "target": "add"}, {"source": "add", "target": "out"}],
    }, name="Ingest")
    run = (await client.post(f"/api/workflows/{ingest}/run?sync=true", json={"inputs": {"document": file_id}},
                             headers=user.headers)).json()
    assert run["status"] == "success", run
    assert run["final_output"]["result"]["status"] == "ready" and run["final_output"]["result"]["chunks"] >= 2

    qa = await create_workflow(client, user, {
        "nodes": [
            {"id": "q", "type": "input", "config": {"name": "question"}},
            {"id": "retriever", "type": "retriever", "config": {"knowledge_base": "Handbook", "query": "{{q.value}}", "top_k": 3}},
            {"id": "reranker", "type": "reranker", "config": {
                "provider": "mock", "query": "{{q.value}}", "results": "{{retriever.results}}", "top_n": 2}},
            {"id": "out", "type": "output", "config": {"value": "{{reranker.results}}"}},
        ],
        "edges": [{"source": "q", "target": "retriever"}, {"source": "retriever", "target": "reranker"},
                  {"source": "reranker", "target": "out"}],
    }, name="Q&A")
    run = (await client.post(f"/api/workflows/{qa}/run?sync=true", json={"inputs": {"question": "tokyo hotel per night"}},
                             headers=user.headers)).json()
    assert run["status"] == "success", run
    top = run["final_output"]["result"]
    # The mock LLM can't score, so the reranker keeps the retrieval order (reranked: false).
    assert len(top) == 2 and "Tokyo" in top[0]["content"] and top[0]["citation"] == "[1]"


async def test_validation_flags_a_retriever_without_a_knowledge_base(client, user):
    wid = await create_workflow(client, user, {
        "nodes": [{"id": "retriever", "type": "retriever", "config": {"query": "x"}}], "edges": [],
    })
    result = (await client.post(f"/api/workflows/{wid}/validate", headers=user.headers)).json()
    assert result["valid"] is False
    assert any("knowledge_base" in str(error) for error in result["errors"])
