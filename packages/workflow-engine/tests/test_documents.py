"""Document AI nodes: PDF Extract and OCR on real files (PyMuPDF, Tesseract), Summarize and
Entity Extraction against scripted LLM providers, and Input nodes of type file."""

import json
import shutil

import pytest

from flowforge_engine import (
    ExecutionServices,
    LocalFileStore,
    NodeStatus,
    ProviderSettings,
    RunStatus,
    WorkflowGraph,
    execute_graph,
    execute_node,
)
from flowforge_engine.errors import ProviderError
from flowforge_engine.nodes.documents import parse_custom_types, parse_entities, parse_pages
from flowforge_engine.testing import chain, make_context, node

pymupdf = pytest.importorskip("pymupdf")
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")


class ScriptedLLM:
    """An LLM provider that returns (or raises) the scripted replies in order."""

    is_mock = False

    def __init__(self, name, *replies):
        self.name = name
        self.replies = list(replies)
        self.prompts = []

    async def generate(self, system_prompt, user_prompt, model, temperature, max_tokens):
        self.prompts.append({"system": system_prompt, "user": user_prompt, "model": model, "temperature": temperature})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def stream(self, **request):  # pragma: no cover - not used here
        yield await self.generate(**request)

    async def embed(self, text, model=None):  # pragma: no cover
        raise NotImplementedError

    async def verify(self, model=None):  # pragma: no cover
        return {}


def services(files=None, **llms):
    return ExecutionServices(provider_settings=ProviderSettings(testing=True), llm_providers=llms or None, files=files)


def text_pdf(path, pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 100), text, fontsize=14)
    doc.save(path)
    return path


def scanned_pdf(path, text, dpi=200):
    """An image-only PDF of `text` (what a scanner produces): no text layer at all."""
    src = pymupdf.open()
    page = src.new_page()
    page.insert_text((60, 120), text, fontsize=22)
    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    out = pymupdf.open()
    target = out.new_page(width=page.rect.width, height=page.rect.height)
    target.insert_image(target.rect, stream=pixmap.tobytes("png"))
    out.save(path)
    return path


def text_png(path, text):
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=160)
    page.insert_text((20, 90), text, fontsize=26)
    page.get_pixmap(dpi=200).save(path)
    return path


@pytest.fixture
def store():
    return LocalFileStore()


# --- page ranges -----------------------------------------------------------------------


def test_page_ranges():
    assert parse_pages(None, 3) == [0, 1, 2]
    assert parse_pages("2", 3) == [1]
    assert parse_pages("1-2, 3", 5) == [0, 1, 2]
    assert parse_pages("2-", 4) == [1, 2, 3]
    assert parse_pages("3-9", 4) == [2, 3]  # the end is clamped
    with pytest.raises(ValueError, match="page 7 is past the end"):
        parse_pages("7", 4)
    with pytest.raises(ValueError, match="invalid page range"):
        parse_pages("3-1", 4)


# --- PDF Extract -------------------------------------------------------------------------


async def test_pdf_extract_reads_the_text_layer_and_page_ranges(tmp_path, store):
    pages = ["Alpha: the first page of the report.", "Beta: the second page of the report.",
             "Gamma: the third page of the report."]
    pdf = store.add(text_pdf(tmp_path / "report.pdf", pages))
    result = await execute_node(node("pdf", "pdf_extract", file=pdf.id, pages="2-"), make_context(services=services(store)))
    assert result.status is NodeStatus.SUCCESS, result.error
    out = result.output
    assert out["page_count"] == 3 and out["pages_extracted"] == [2, 3]
    assert out["text"] == f"{pages[1]}\n\n{pages[2]}" and out["needs_ocr"] is False and out["filename"] == "report.pdf"
    assert out["pages"][0] == {"page": 2, "text": pages[1], "chars": len(pages[1])}


async def test_pdf_extract_accepts_the_file_object_from_an_input_node(tmp_path, store):
    pdf = store.add(text_pdf(tmp_path / "a.pdf", ["Hello"]))
    ctx = make_context(services=services(store), node_outputs={"input": {"value": pdf.describe()}})
    result = await execute_node(node("pdf", "pdf_extract", file="{{input.value}}", max_chars=3), ctx)
    assert result.output["text"] == "Hel" and result.output["truncated"] is True


async def test_pdf_extract_flags_scans_and_rejects_other_files(tmp_path, store):
    scan = store.add(scanned_pdf(tmp_path / "scan.pdf", "Scanned words"))
    result = await execute_node(node("pdf", "pdf_extract", file=scan.id), make_context(services=services(store)))
    assert result.output["char_count"] == 0 and result.output["needs_ocr"] is True

    png = store.add(text_png(tmp_path / "x.png", "hi"))
    wrong = await execute_node(node("pdf", "pdf_extract", file=png.id), make_context(services=services(store)))
    assert wrong.status is NodeStatus.FAILED and "is image/png; this node reads application/pdf" in wrong.error

    missing = await execute_node(node("pdf", "pdf_extract", file="nope"), make_context(services=services(store)))
    assert missing.status is NodeStatus.FAILED and "was not found" in missing.error

    past_end = await execute_node(node("pdf", "pdf_extract", file=scan.id, pages="5"), make_context(services=services(store)))
    assert "page 5 is past the end (the document has 1 page)" in past_end.error


async def test_document_nodes_need_a_file_store():
    result = await execute_node(node("pdf", "pdf_extract", file="abc"), make_context())
    assert result.status is NodeStatus.FAILED and "File storage isn't available" in result.error


def test_page_specs_are_validated_with_the_config():
    from flowforge_engine import validate_workflow

    graph = WorkflowGraph(nodes=[node("p", "pdf_extract", file="x", pages="one")])
    issues = validate_workflow(graph)
    assert any(i.field == "pages" and "like '1-3,5'" in i.message for i in issues)


# --- OCR ---------------------------------------------------------------------------------


@needs_tesseract
async def test_ocr_reads_a_scanned_pdf(tmp_path, store):
    scan = store.add(scanned_pdf(tmp_path / "scan.pdf", "Invoice 4417 due March"))
    result = await execute_node(node("ocr", "ocr", file=scan.id, dpi=200), make_context(services=services(store)))
    assert result.status is NodeStatus.SUCCESS, result.error
    out = result.output
    assert "Invoice 4417 due March" in out["text"]
    assert out["source"] == "pdf" and out["pages_processed"] == [1] and out["language"] == "eng"
    assert out["mean_confidence"] > 60 and out["engine"].startswith("tesseract 5")


@needs_tesseract
async def test_ocr_reads_images(tmp_path, store):
    png = store.add(text_png(tmp_path / "sign.png", "Harbour Freight"))
    result = await execute_node(node("ocr", "ocr", file=png.id), make_context(services=services(store)))
    assert result.status is NodeStatus.SUCCESS, result.error
    assert "Harbour Freight" in result.output["text"] and result.output["source"] == "image"


@needs_tesseract
async def test_ocr_reports_missing_language_data(tmp_path, store):
    png = store.add(text_png(tmp_path / "x.png", "text"))
    result = await execute_node(node("ocr", "ocr", file=png.id, language="xxx"), make_context(services=services(store)))
    assert result.status is NodeStatus.FAILED
    assert "language data not installed: xxx" in result.error and "eng" in result.error


@needs_tesseract
async def test_ocr_refuses_documents_over_max_pages(tmp_path, store):
    pdf = store.add(text_pdf(tmp_path / "long.pdf", ["a", "b", "c"]))
    result = await execute_node(node("ocr", "ocr", file=pdf.id, max_pages=2), make_context(services=services(store)))
    assert result.status is NodeStatus.FAILED and "more than max_pages (2)" in result.error


# --- Summarize ---------------------------------------------------------------------------


async def test_summarize_prompts_for_length_style_and_focus():
    llm = ScriptedLLM("gemini", "  The invoice totals EUR 4,389.20.  ")
    config = {"text": "Invoice text " * 10, "length": "short", "style": "bullets", "focus": "the total"}
    result = await execute_node(node("sum", "summarize", **config), make_context(services=services(gemini=llm)))
    assert result.status is NodeStatus.SUCCESS, result.error
    out = result.output
    assert out["summary"] == "The invoice totals EUR 4,389.20." and out["provider_used"] == "gemini" and not out["truncated"]
    prompt = llm.prompts[0]["user"]
    assert "a bulleted list" in prompt and "2-3 sentences" in prompt and "Focus on: the total." in prompt
    assert "<document>\nInvoice text" in prompt


async def test_summarize_truncates_long_input_and_uses_the_fallback_chain():
    failing = ScriptedLLM("gemini", ProviderError("gemini", "429 quota exceeded", retryable=True))
    backup = ScriptedLLM("groq", "Short summary.")
    config = {"text": "x" * 5000, "max_input_chars": 1000, "fallback": ["groq:llama-3.1-8b-instant"]}
    result = await execute_node(node("sum", "summarize", **config), make_context(services=services(gemini=failing, groq=backup)))
    out = result.output
    assert out["provider_used"] == "groq" and out["model"] == "llama-3.1-8b-instant"
    assert out["truncated"] is True and out["input_chars"] == 1000
    assert out["fallback_errors"][0]["provider"] == "gemini"
    assert "The document was cut short" in backup.prompts[0]["user"]


async def test_summarize_fails_on_empty_text():
    result = await execute_node(node("sum", "summarize", text="   ", provider="mock"), make_context())
    assert result.status is NodeStatus.FAILED and "no text to summarize" in result.error


# --- Entity Extraction -------------------------------------------------------------------

GOOD_REPLY = {
    "people": [{"name": "Maria Schneider", "role": "Head of Procurement"}],
    "organizations": [{"name": "Bluefield Retail GmbH", "kind": "company"}],
    "dates": [{"text": "12 March 2026", "iso": "2026-03-12", "meaning": "invoice date"}],
    "amounts": [{"text": "EUR 4,389.20", "value": "4,389.20", "currency": "EUR", "meaning": "total due"}],
    "custom": {"invoice_number": [{"text": "HF-2026-0417"}], "unrequested": [{"text": "x"}]},
}


async def test_entities_are_validated_and_normalized():
    llm = ScriptedLLM("gemini", "```json\n" + json.dumps(GOOD_REPLY) + "\n```")
    config = {"text": "An invoice.", "custom_types": ["invoice_number: the invoice id"]}
    result = await execute_node(node("ent", "extract_entities", **config), make_context(services=services(gemini=llm)))
    assert result.status is NodeStatus.SUCCESS, result.error
    out = result.output
    assert out["attempts"] == 1
    entities = out["entities"]
    assert entities["amounts"][0]["value"] == 4389.2  # "4,389.20" coerced to a number
    assert entities["people"][0] == {"name": "Maria Schneider", "role": "Head of Procurement"}
    assert entities["custom"] == {"invoice_number": [{"text": "HF-2026-0417", "note": None}]}  # only what was asked
    assert out["counts"] == {"people": 1, "organizations": 1, "dates": 1, "amounts": 1, "custom.invoice_number": 1}
    prompt = llm.prompts[0]["user"]
    assert '"custom": {"invoice_number"' in prompt and "custom.invoice_number: the invoice id" in prompt
    assert llm.prompts[0]["temperature"] == 0.0


async def test_invalid_json_gets_one_retry_with_the_problem():
    llm = ScriptedLLM("gemini", "Sure! Here are the entities: people are Maria.", json.dumps(GOOD_REPLY))
    result = await execute_node(node("ent", "extract_entities", text="doc"), make_context(services=services(gemini=llm)))
    assert result.status is NodeStatus.SUCCESS and result.output["attempts"] == 2
    retry = llm.prompts[1]["user"]
    assert "Your previous reply could not be used: the reply contains no JSON object" in retry


async def test_schema_violations_twice_fail_the_node():
    bad = {**GOOD_REPLY, "dates": [{"text": "spring", "iso": "sometime in 2026"}]}
    llm = ScriptedLLM("gemini", json.dumps(bad), json.dumps(bad))
    result = await execute_node(node("ent", "extract_entities", text="doc"), make_context(services=services(gemini=llm)))
    assert result.status is NodeStatus.FAILED
    assert "after 2 attempts" in result.error and "dates.0.iso" in result.error
    assert "YYYY-MM-DD" in llm.prompts[1]["user"]  # the retry said what was wrong
    assert result.output["attempts"] == 2 and "sometime in 2026" in result.output["raw_reply"]


async def test_only_the_requested_types_are_kept():
    entities = parse_entities(json.dumps(GOOD_REPLY), ["people"], {})
    assert entities.people and not entities.amounts and entities.custom == {}
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_entities("{people: [}", ["people"], {})
    with pytest.raises(ValueError, match="lowercase letters"):
        parse_custom_types(["Invoice Number!"])


# --- Input nodes of type file, and a whole document pipeline ---------------------------


async def test_file_inputs_output_the_file_description(tmp_path, store):
    pdf = store.add(text_pdf(tmp_path / "in.pdf", ["x"]))
    ctx = make_context(services=services(store), inputs={"document": pdf.id})
    result = await execute_node(node("input", "input", name="document", input_type="file"), ctx)
    assert result.output["document"] == {
        "file_id": pdf.id, "filename": "in.pdf", "content_type": "application/pdf", "size_bytes": pdf.size_bytes,
    }
    missing = await execute_node(node("input", "input", name="document", input_type="file"),
                                 make_context(services=services(store), inputs={"document": "gone"}))
    assert missing.status is NodeStatus.FAILED and "Input 'document': File 'gone' was not found" in missing.error


async def test_a_document_pipeline_end_to_end(tmp_path, store):
    pdf = store.add(text_pdf(tmp_path / "invoice.pdf", ["Invoice HF-1 from Harbourline, total EUR 10.00"]))
    llm = ScriptedLLM("gemini", "Harbourline billed EUR 10.", json.dumps(GOOD_REPLY))
    graph = WorkflowGraph(
        nodes=[
            node("input", "input", name="document", input_type="file"),
            node("pdf", "pdf_extract", file="{{input.document}}"),
            node("sum", "summarize", text="{{pdf.text}}"),
            node("ent", "extract_entities", text="{{pdf.text}}"),
            node("out", "output", value={"summary": "{{sum.summary}}", "entities": "{{ent.entities}}"}),
        ],
        edges=chain("input", "pdf", "sum", "ent", "out"),
    )
    result = await execute_graph(graph, make_context(services=services(store, gemini=llm), inputs={"document": pdf.id}))
    assert result.status is RunStatus.SUCCESS, result.error
    final = result.final_output["result"]
    assert final["summary"] == "Harbourline billed EUR 10." and final["entities"]["people"][0]["name"] == "Maria Schneider"
    assert "Invoice HF-1 from Harbourline" in llm.prompts[0]["user"]
