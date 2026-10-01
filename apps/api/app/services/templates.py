"""Ready-made pipelines ("Use template" on the dashboard).

The catalog below is the source of truth: `sync_templates` writes it into the `templates`
table (on API start and by the seed). Using a template copies its graph into a new
workflow the user owns and can edit, fitted to what they have connected:

- LLM steps use the first free provider with credentials (Gemini, then Groq, then
  OpenRouter) and fall back to the others that have them;
- a Telegram step becomes a Discord Webhook step when only Discord is connected, or a
  Gmail step (to the user's own address) when only Gmail is;
- Speech to Text uses Groq's Whisper with a Groq key, local faster-whisper without one;
- the Invoice Extractor and Meeting Notes file inputs default to the bundled samples;
- its triggers are created switched off, with the schedule in the user's time zone;
- the knowledge templates create the "My documents" knowledge base if it's missing.

Each template lists the credentials it needs, and the dashboard shows which are missing.
"""

import copy
import logging
import uuid
from typing import Any

from flowforge_engine import ExecutionServices, WorkflowGraph
from flowforge_engine.knowledge import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TriggerType, WorkflowStatus
from app.models.template import Template
from app.models.trigger import WorkflowTrigger
from app.models.user import User
from app.models.workflow import Workflow
from app.services.credentials import build_execution_services
from app.services.files import ensure_sample_file
from app.services.knowledge import create_kb, owned_kb
from app.services.schedule import ScheduleError, zone
from app.services.workflows import replace_graph

logger = logging.getLogger(__name__)

# Free LLM providers in order of preference (Ollama is free too, but may not be running).
FREE_LLMS = ("gemini", "groq", "openrouter")
LLM_NODE_TYPES = frozenset({"gemini", "groq", "openrouter", "ollama", "openai", "anthropic"})
# Other nodes with a provider/fallback chain.
LLM_STEP_TYPES = frozenset({"for_each", "summarize", "extract_entities", "structured_output", "reranker"})
SAMPLE_INVOICE = "scanned-invoice.pdf"
# A made-up planning meeting voiced by Windows text-to-speech (samples/make_meeting_sample.ps1).
SAMPLE_MEETING = "team-meeting.mp3"
# Which bundled sample a template's file input defaults to.
SAMPLE_FILES = {"invoice-extractor": SAMPLE_INVOICE, "meeting-notes": SAMPLE_MEETING}

LLM_REQUIREMENT = {
    "providers": list(FREE_LLMS),
    "label": "An LLM key: Gemini, Groq, or OpenRouter",
    "why": "Free keys work; with more than one, the others become fallbacks.",
}
NOTIFY_REQUIREMENT = {
    "providers": ["telegram", "discord"],
    "label": "Telegram bot (or a Discord webhook)",
    "why": "Where the result is sent. With only Discord connected, the step is created as Discord Webhook.",
}
NOTES_DELIVERY_REQUIREMENT = {
    "providers": ["telegram", "discord", "gmail"],
    "label": "Telegram, Discord, or Gmail",
    "why": "Where the notes go: the first of these you have connected.",
}
SPEECH_REQUIREMENT = {
    "providers": ["groq", "local"],
    "label": "Speech-to-text: a Groq key, or local faster-whisper",
    "why": "Groq's free Whisper tier is fast. Without a key, the step uses faster-whisper on the worker's CPU (no key; slower).",
}
SEARCH_REQUIREMENT = {
    "providers": ["duckduckgo", "tavily"],
    "label": "Web search: DuckDuckGo (no key), or Tavily",
    "why": "DuckDuckGo needs nothing. With TAVILY_API_KEY set, searches fall back to Tavily when DuckDuckGo rate limits.",
}
GMAIL_REQUIREMENT = {"providers": ["gmail"], "label": "Gmail (App Password)"}
EMBEDDING_REQUIREMENT = {
    "providers": ["gemini", "openai"],
    "label": "Embeddings: a Gemini key (or OpenAI)",
    "why": "Turns document chunks and questions into vectors. Without one, the knowledge base uses mock word-matching embeddings.",
}
# Templates that work on a knowledge base: using one creates DEFAULT_KB if it's missing.
KB_TEMPLATES = frozenset({"pdf-to-knowledge-base", "document-qa"})
DEFAULT_KB = "My documents"

SAMPLE_EMAIL = {
    "from": "Priya Raman <priya@example.com>",
    "from_address": "priya@example.com",
    "to": ["you@example.com"],
    "subject": "URGENT: checkout is down for all customers",
    "date": "2026-09-28T09:14:00+00:00",
    "body_text": (
        "Hi, since 09:05 every checkout fails with a 500 error and customers are emailing support. "
        "Can you join the incident call now? We need a decision on rolling back before 10:00."
    ),
    "snippet": "Hi, since 09:05 every checkout fails with a 500 error...",
    "message_id": "<sample-urgent@example.com>",
    "attachments": [],
}

SAMPLE_RESUME = (
    "Backend engineer, 6 years. Python (FastAPI, Django), PostgreSQL, Redis, Celery, Docker, AWS. "
    "Built payment and data pipelines; some React. Looking for remote senior backend or platform roles; "
    "not interested in frontend-only, sales, or on-site jobs."
)


def _pos(x: int, y: int) -> dict[str, int]:
    return {"x": x, "y": y}


MORNING_DIGEST: dict[str, Any] = {
    "nodes": [
        {
            "id": "inbox", "type": "gmail_read", "label": "Unread email", "position": _pos(0, 0),
            "description": "Unread mail from the last day",
            "config": {"auth": "gmail", "unread_only": True, "since_days": 1, "max_results": 10, "max_body_chars": 1500},
        },
        {
            "id": "news", "type": "rss", "label": "News feed", "position": _pos(0, 180),
            "description": "New stories since the last digest",
            "config": {"url": "{{vars.feed_url}}", "max_items": 8, "since_last_run": True, "max_summary_chars": 600},
        },
        {
            "id": "summaries", "type": "for_each", "label": "Summarize each", "position": _pos(320, 90),
            "description": "One line per email and story (rate limited for free tiers)",
            "config": {
                "items": ["{{inbox.emails}}", "{{news.items}}"],
                "flatten": True,
                "mode": "llm",
                "provider": "gemini",
                "system_prompt": "You write crisp one-sentence summaries. Reply with the sentence only.",
                "prompt": (
                    "Summarize the item below in one sentence of at most 30 words. If it is an email (it has a "
                    "\"from\" field), start with \"Email from <sender name>:\". If it is a news story, start with "
                    "\"News:\".\n\n{{item}}"
                ),
                "temperature": 0.2,
                "max_tokens": 1024,
                "concurrency": 2,
                "rate_limit_per_minute": 10,
                "max_items": 20,
            },
        },
        {
            "id": "bullets", "type": "join", "label": "Bullet list", "position": _pos(640, 90),
            "config": {
                "items": "{{summaries.outputs}}", "template": "- {{item}}", "separator": "\\n",
                "empty_text": "(No unread email and no new stories.)",
            },
        },
        {
            "id": "digest", "type": "gemini", "label": "Write the digest", "position": _pos(960, 90),
            "config": {
                "provider": "gemini",
                "system_prompt": "You write a short, friendly morning briefing in Markdown.",
                "user_prompt": (
                    "Write my morning digest from these one-line summaries. Put the emails under a bold **Inbox** "
                    "heading and the stories under **News**, one short bullet each, and end with one line saying "
                    "what to look at first. If there is nothing, say so in one friendly sentence.\n\n{{bullets.text}}"
                ),
                "temperature": 0.4,
                "max_tokens": 2048,
            },
        },
        {
            "id": "send", "type": "telegram", "label": "Send to Telegram", "position": _pos(1280, 90),
            "config": {"text": "☀️ **Morning digest**\n\n{{digest.response}}", "format": "markdown"},
        },
        {
            "id": "out", "type": "output", "label": "Digest", "position": _pos(1600, 90),
            "config": {
                "name": "digest",
                "value": {
                    "digest": "{{digest.response}}", "emails": "{{inbox.count}}", "stories": "{{news.count}}",
                    "messages": "{{send.message_ids}}",
                },
            },
        },
    ],
    "edges": [
        {"source": "inbox", "target": "summaries"},
        {"source": "news", "target": "summaries"},
        {"source": "summaries", "target": "bullets"},
        {"source": "bullets", "target": "digest"},
        {"source": "digest", "target": "send"},
        {"source": "send", "target": "out"},
    ],
    "variables": [{"key": "feed_url", "value": "https://feeds.bbci.co.uk/news/technology/rss.xml", "type": "workflow"}],
}

INVOICE_EXTRACTOR: dict[str, Any] = {
    "nodes": [
        {
            "id": "input", "type": "input", "label": "Invoice file", "position": _pos(0, 60),
            "description": "A PDF (scanned or digital) or an image. Defaults to the sample scan.",
            "config": {"name": "document", "input_type": "file"},
        },
        {
            "id": "read", "type": "ocr", "label": "Read the text", "position": _pos(320, 60),
            "description": "Text layer where the PDF has one, Tesseract OCR for scanned pages",
            "config": {"file": "{{input.document}}", "language": "eng", "dpi": 300, "prefer_text_layer": True},
        },
        {
            "id": "entities", "type": "extract_entities", "label": "Extract fields", "position": _pos(640, 60),
            "description": "Validated JSON: parties, dates, amounts, and invoice fields",
            "config": {
                "text": "{{read.text}}",
                "provider": "gemini",
                "custom_types": [
                    "invoice_number: the invoice or reference number",
                    "vendor: the company that issued the invoice",
                    "customer: the company or person being billed",
                    "due_date: when payment is due",
                    "total_due: the total amount to pay, with currency",
                ],
            },
        },
        {
            "id": "out", "type": "output", "label": "Invoice JSON", "position": _pos(960, 60),
            "config": {
                "name": "invoice",
                "value": {
                    "file": "{{read.filename}}", "pages": "{{read.page_count}}", "entities": "{{entities.entities}}",
                    "counts": "{{entities.counts}}",
                },
            },
        },
    ],
    "edges": [
        {"source": "input", "target": "read"},
        {"source": "read", "target": "entities"},
        {"source": "entities", "target": "out"},
    ],
    "variables": [],
}

EMAIL_TRIAGE: dict[str, Any] = {
    "nodes": [
        {
            "id": "email", "type": "input", "label": "Incoming email", "position": _pos(0, 80),
            "description": "The email trigger passes each new email here (a sample for manual runs)",
            "config": {"name": "email", "input_type": "json", "required": False, "default": SAMPLE_EMAIL},
        },
        {
            "id": "classify", "type": "gemini", "label": "Classify", "position": _pos(320, 80),
            "config": {
                "provider": "gemini",
                "system_prompt": "You triage email. Reply with exactly one lowercase word: urgent, normal, or spam.",
                "user_prompt": (
                    "Classify this email.\n- urgent: needs action within hours (an outage, a same-day deadline, "
                    "security, someone blocked waiting on me)\n- spam: unsolicited marketing, scams, phishing\n"
                    "- normal: everything else\n\nFrom: {{email.value.from}}\nSubject: {{email.value.subject}}\n\n"
                    "{{email.value.body_text}}"
                ),
                "temperature": 0,
                "max_tokens": 1024,
            },
        },
        {
            "id": "is_urgent", "type": "condition", "label": "Urgent?", "position": _pos(640, 80),
            "config": {"left": "{{classify.response}}", "operator": "contains", "right": "urgent", "case_sensitive": False},
        },
        {
            "id": "notify", "type": "telegram", "label": "Alert on Telegram", "position": _pos(960, 0),
            "config": {
                "text": (
                    "🚨 **Urgent email**\nFrom: {{email.value.from}}\nSubject: {{email.value.subject}}\n\n"
                    "{{email.value.body_text}}"
                ),
                "format": "markdown",
            },
        },
        {
            "id": "out", "type": "output", "label": "Triage result", "position": _pos(1280, 80),
            "config": {
                "name": "triage",
                "value": {
                    "category": "{{classify.response}}", "subject": "{{email.value.subject}}",
                    "from": "{{email.value.from}}",
                },
            },
        },
    ],
    "edges": [
        {"source": "email", "target": "classify"},
        {"source": "classify", "target": "is_urgent"},
        {"source": "is_urgent", "target": "notify", "source_handle": "true"},
        {"source": "is_urgent", "target": "out", "source_handle": "false"},
        {"source": "notify", "target": "out"},
    ],
    "variables": [],
}

JOB_ALERT: dict[str, Any] = {
    "nodes": [
        {
            "id": "jobs", "type": "rss", "label": "Job feed", "position": _pos(0, 80),
            "description": "New postings since the last run",
            "config": {"url": "{{vars.feed_url}}", "max_items": 15, "since_last_run": True, "max_summary_chars": 1500},
        },
        {
            "id": "score", "type": "for_each", "label": "Score against resume", "position": _pos(320, 80),
            "description": "0-100 per posting, as JSON (rate limited for free tiers)",
            "config": {
                "items": "{{jobs.items}}",
                "mode": "llm",
                "provider": "gemini",
                "output_format": "json",
                "system_prompt": "You match job postings to a candidate. You reply with JSON only.",
                "prompt": (
                    "Score how well this job fits the candidate from 0 (no fit) to 100 (perfect fit), considering "
                    "skills, seniority, and remote or location requirements.\n\nCandidate:\n{{vars.resume}}\n\n"
                    "Job: {{item.title}}\n{{item.summary}}\n\n"
                    "Reply with only this JSON: {\"score\": <0-100>, \"reason\": \"<one short sentence>\"}"
                ),
                "temperature": 0,
                "max_tokens": 1024,
                "concurrency": 2,
                "rate_limit_per_minute": 10,
                "max_items": 15,
            },
        },
        {
            "id": "matches", "type": "filter", "label": "Good matches", "position": _pos(640, 80),
            "config": {"items": "{{score.results}}", "field": "output.score", "operator": "greater_or_equal",
                       "value": "{{vars.threshold}}"},
        },
        {
            "id": "any", "type": "condition", "label": "Any?", "position": _pos(960, 80),
            "config": {"left": "{{matches.count}}", "operator": "greater_than", "right": 0},
        },
        {
            "id": "message", "type": "join", "label": "Format matches", "position": _pos(1280, 0),
            "config": {
                "items": "{{matches.items}}",
                "template": "**{{item.output.score}}** · [{{item.item.title}}]({{item.item.link}})\n{{item.output.reason}}",
                "separator": "\\n\\n",
                "header": "🎯 **{{matches.count}} new job matches**\n",
            },
        },
        {
            "id": "notify", "type": "telegram", "label": "Send matches", "position": _pos(1600, 0),
            "config": {"text": "{{message.text}}", "format": "markdown"},
        },
        {
            "id": "out", "type": "output", "label": "Summary", "position": _pos(1920, 80),
            "config": {"name": "jobs", "value": {"new_postings": "{{jobs.count}}", "scored": "{{score.succeeded}}",
                                                 "matches": "{{matches.count}}"}},
        },
    ],
    "edges": [
        {"source": "jobs", "target": "score"},
        {"source": "score", "target": "matches"},
        {"source": "matches", "target": "any"},
        {"source": "any", "target": "message", "source_handle": "true"},
        {"source": "any", "target": "out", "source_handle": "false"},
        {"source": "message", "target": "notify"},
        {"source": "notify", "target": "out"},
    ],
    "variables": [
        {"key": "feed_url", "value": "https://weworkremotely.com/categories/remote-programming-jobs.rss", "type": "workflow"},
        {"key": "resume", "value": SAMPLE_RESUME, "type": "workflow"},
        {"key": "threshold", "value": "70", "type": "workflow"},
    ],
}

MEETING_NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "decisions", "action_items"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "minLength": 1, "description": "3 to 5 sentences"},
        "decisions": {"type": "array", "items": {"type": "string"}, "description": "What was decided, one per item"},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["task", "owner", "due"],
                "additionalProperties": False,
                "properties": {
                    "task": {"type": "string", "minLength": 1},
                    "owner": {"type": "string", "description": "the person named, or 'unassigned'"},
                    "due": {"type": "string", "description": "as said, e.g. 'Friday', or 'not set'"},
                },
            },
        },
    },
}

MEETING_NOTES: dict[str, Any] = {
    "nodes": [
        {
            "id": "input", "type": "input", "label": "Recording", "position": _pos(0, 80),
            "description": "An audio or video file of the meeting (or record one in the Run form). Defaults to the sample call.",
            "config": {"name": "recording", "input_type": "file"},
        },
        {
            "id": "stt", "type": "speech_to_text", "label": "Transcribe", "position": _pos(320, 80),
            "description": "Whisper, with timestamps; long recordings are chunked (runs on worker-audio)",
            "config": {"file": "{{input.recording}}", "provider": "groq", "prompt": "{{vars.vocabulary}}"},
        },
        {
            "id": "notes", "type": "structured_output", "label": "Meeting notes", "position": _pos(640, 80),
            "description": "Summary, decisions, and action items as validated JSON",
            "config": {
                "provider": "gemini",
                "prompt": (
                    "Write notes for people who missed this meeting. From the transcript below, give a summary (3 to 5 "
                    "sentences), the decisions that were made, and the action items with their owner and due date. Only "
                    "use what was said. Use \"unassigned\" when no owner is named and \"not set\" when no date is given. "
                    "The transcript has no speaker labels, so don't guess who said what beyond the names used.\n\n"
                    "Transcript:\n{{stt.text}}"
                ),
                "schema": MEETING_NOTES_SCHEMA,
                "temperature": 0.1,
            },
        },
        {
            "id": "decisions", "type": "join", "label": "Decisions", "position": _pos(960, 0),
            "config": {"items": "{{notes.data.decisions}}", "template": "- {{item}}", "empty_text": "- (none)"},
        },
        {
            "id": "actions", "type": "join", "label": "Action items", "position": _pos(1280, 0),
            "config": {
                "items": "{{notes.data.action_items}}",
                "template": "- {{item.task}} (owner: {{item.owner}}, due: {{item.due}})",
                "empty_text": "- (none)",
            },
        },
        {
            "id": "send", "type": "telegram", "label": "Send the notes", "position": _pos(1600, 80),
            "email_subject": "Meeting notes: {{stt.filename}}",
            "config": {
                "text": (
                    "📝 **Meeting notes: {{stt.filename}}**\n\n{{notes.data.summary}}\n\n**Decisions**\n{{decisions.text}}"
                    "\n\n**Action items**\n{{actions.text}}"
                ),
                "format": "markdown",
            },
        },
        {
            "id": "out", "type": "output", "label": "Notes and transcript", "position": _pos(1920, 80),
            "config": {
                "name": "meeting",
                "value": {
                    "notes": "{{notes.data}}", "language": "{{stt.language}}", "duration_seconds": "{{stt.duration_seconds}}",
                    "transcript": "{{stt.text}}", "segments": "{{stt.segments}}",
                },
            },
        },
    ],
    "edges": [
        {"source": "input", "target": "stt"},
        {"source": "stt", "target": "notes"},
        {"source": "notes", "target": "decisions"},
        {"source": "decisions", "target": "actions"},
        {"source": "actions", "target": "send"},
        {"source": "send", "target": "out"},
    ],
    "variables": [{"key": "vocabulary", "value": "FlowForge, Groq, Telegram, Whisper", "type": "workflow"}],
}

WEB_RESEARCH: dict[str, Any] = {
    "nodes": [
        {
            "id": "question", "type": "input", "label": "Question", "position": _pos(0, 80),
            "config": {"name": "question", "input_type": "text",
                       "default": "What changed between HTTP/2 and HTTP/3, and why does HTTP/3 run over UDP?"},
        },
        {
            "id": "search", "type": "web_search", "label": "Search the web", "position": _pos(320, 80),
            "description": "DuckDuckGo (Tavily as the fallback when it has a key)",
            "config": {"query": "{{question.value}}", "max_results": 5},
        },
        {
            "id": "top", "type": "web_page", "label": "Read the top result", "position": _pos(640, 80),
            "description": "Full text of result 1 (a site that blocks readers gives empty text, not a failure)",
            "config": {"url": "{{search.results[0].url}}", "max_chars": 8000, "fail_on_error": False},
        },
        {
            "id": "sources", "type": "join", "label": "Number the sources", "position": _pos(960, 80),
            "config": {
                "items": "{{search.results}}", "template": "{{item.title}}\nURL: {{item.url}}\n{{item.snippet}}",
                "separator": "\\n\\n", "numbered": True,
            },
        },
        {
            "id": "answer", "type": "gemini", "label": "Answer with citations", "position": _pos(1280, 80),
            "config": {
                "provider": "gemini",
                "system_prompt": "You answer questions from web sources and cite them. You never invent sources.",
                "user_prompt": (
                    "Question: {{question.value}}\n\nSources, numbered:\n{{sources.text}}\n\nFull text of source 1:\n"
                    "{{top.text}}\n\nAnswer the question in 2 to 4 short paragraphs using only these sources, citing them "
                    "inline by number as [1], [2], and so on. End with a line \"Sources:\" followed by each source you "
                    "cited as \"[n] Title - URL\". If the sources don't answer the question, say so."
                ),
                "temperature": 0.2,
                "max_tokens": 2048,
            },
        },
        {
            "id": "out", "type": "output", "label": "Answer", "position": _pos(1600, 80),
            "config": {
                "name": "research",
                "value": {"answer": "{{answer.response}}", "sources": "{{search.results}}",
                          "search_provider": "{{search.provider_used}}"},
            },
        },
    ],
    "edges": [
        {"source": "question", "target": "search"},
        {"source": "search", "target": "top"},
        {"source": "top", "target": "sources"},
        {"source": "sources", "target": "answer"},
        {"source": "answer", "target": "out"},
    ],
    "variables": [],
}

PDF_TO_KB: dict[str, Any] = {
    "nodes": [
        {
            "id": "document", "type": "input", "label": "Document", "position": _pos(0, 80),
            "config": {"name": "document", "input_type": "file"},
        },
        {
            "id": "add", "type": "kb_add_document", "label": "Add to the knowledge base", "position": _pos(320, 80),
            "description": "Text layer (OCR for scanned pages), chunked, embedded, stored",
            "config": {"knowledge_base": "{{vars.knowledge_base}}", "file": "{{document.value}}"},
        },
        {
            "id": "out", "type": "output", "label": "Ready", "position": _pos(640, 80),
            "config": {
                "name": "document",
                "value": {"status": "{{add.status}}", "filename": "{{add.filename}}", "chunks": "{{add.chunk_count}}",
                          "read_with": "{{add.method}}", "knowledge_base": "{{add.knowledge_base}}",
                          "document_id": "{{add.document_id}}"},
            },
        },
    ],
    "edges": [{"source": "document", "target": "add"}, {"source": "add", "target": "out"}],
    "variables": [{"key": "knowledge_base", "value": DEFAULT_KB, "type": "workflow"}],
}

DOCUMENT_QA: dict[str, Any] = {
    "nodes": [
        {
            "id": "question", "type": "input", "label": "Question", "position": _pos(0, 80),
            "config": {"name": "question", "input_type": "text", "default": "What does the document say about deadlines?"},
        },
        {
            "id": "retriever", "type": "retriever", "label": "Find relevant chunks", "position": _pos(320, 80),
            "config": {"knowledge_base": "{{vars.knowledge_base}}", "query": "{{question.value}}", "top_k": 8},
        },
        {
            "id": "reranker", "type": "reranker", "label": "Keep the best", "position": _pos(640, 80),
            "description": "An LLM scores each chunk against the question",
            "config": {"provider": "gemini", "query": "{{question.value}}", "results": "{{retriever.results}}", "top_n": 4},
        },
        {
            "id": "answer", "type": "gemini", "label": "Answer with citations", "position": _pos(960, 80),
            "config": {
                "provider": "gemini",
                "system_prompt": "You answer questions from the user's documents and cite them. You never invent facts or sources.",
                "user_prompt": (
                    "Question: {{question.value}}\n\nExcerpts from the documents, numbered:\n{{reranker.context}}\n\n"
                    "Answer using only these excerpts, citing them inline by number as [1], [2], and so on. End with a "
                    "line \"Sources:\" followed by each excerpt you cited as \"[n] file, page\" (leave out the page "
                    "when there is none). If the excerpts don't answer the question, say so."
                ),
                "temperature": 0.2,
                "max_tokens": 2048,
            },
        },
        {
            "id": "out", "type": "output", "label": "Answer", "position": _pos(1280, 80),
            "config": {
                "name": "answer",
                "value": {"answer": "{{answer.response}}", "sources": "{{reranker.results}}",
                          "reranked": "{{reranker.reranked}}"},
            },
        },
    ],
    "edges": [
        {"source": "question", "target": "retriever"},
        {"source": "retriever", "target": "reranker"},
        {"source": "reranker", "target": "answer"},
        {"source": "answer", "target": "out"},
    ],
    "variables": [{"key": "knowledge_base", "value": DEFAULT_KB, "type": "workflow"}],
}

CATALOG: list[dict[str, Any]] = [
    {
        "slug": "morning-digest",
        "name": "Morning Digest",
        "category": "Productivity",
        "description": (
            "Every morning: your unread Gmail and a news feed, each summarized in one line (For Each, rate "
            "limited), joined, written up as a digest, and sent to Telegram."
        ),
        "graph": MORNING_DIGEST,
        "requirements": [
            {**GMAIL_REQUIREMENT, "why": "Reads your unread mail over IMAP."},
            LLM_REQUIREMENT,
            NOTIFY_REQUIREMENT,
        ],
        "triggers": [{"type": "schedule", "config": {"cron": "30 7 * * *", "timezone": "UTC", "inputs": {}}}],
    },
    {
        "slug": "invoice-extractor",
        "name": "Invoice Extractor",
        "category": "Documents",
        "description": (
            "Upload an invoice (PDF or image): the text layer or OCR, then entity extraction as validated JSON. "
            "Download the result as JSON or CSV from the run."
        ),
        "graph": INVOICE_EXTRACTOR,
        "requirements": [LLM_REQUIREMENT],
        "triggers": [],
    },
    {
        "slug": "email-triage",
        "name": "Email Triage",
        "category": "Email",
        "description": (
            "Each new email is classified urgent, normal, or spam by an LLM; only urgent ones reach you on Telegram."
        ),
        "graph": EMAIL_TRIAGE,
        "requirements": [
            {**GMAIL_REQUIREMENT, "why": "The email trigger checks your inbox."},
            LLM_REQUIREMENT,
            NOTIFY_REQUIREMENT,
        ],
        "triggers": [{"type": "email", "config": {"unread_only": True, "poll_minutes": 5, "input_name": "email",
                                                  "max_body_chars": 2000}}],
    },
    {
        "slug": "job-alert-filter",
        "name": "Job Alert Filter",
        "category": "Career",
        "description": (
            "New postings from a jobs RSS feed are scored 0-100 against your resume (a workflow variable); those "
            "at or above the threshold are sent to Telegram or Discord."
        ),
        "graph": JOB_ALERT,
        "requirements": [LLM_REQUIREMENT, NOTIFY_REQUIREMENT],
        "triggers": [{"type": "schedule", "config": {"cron": "0 */6 * * *", "timezone": "UTC", "inputs": {}}}],
    },
    {
        "slug": "meeting-notes",
        "name": "Meeting Notes",
        "category": "Audio",
        "description": (
            "Upload or record a meeting: Whisper transcribes it with timestamps, an LLM writes the summary, decisions, "
            "and action items as validated JSON, and the notes go to Telegram, Discord, or your inbox."
        ),
        "graph": MEETING_NOTES,
        "requirements": [SPEECH_REQUIREMENT, LLM_REQUIREMENT, NOTES_DELIVERY_REQUIREMENT],
        "triggers": [],
    },
    {
        "slug": "web-research",
        "name": "Web Research",
        "category": "Research",
        "description": (
            "Ask a question: the web is searched, the top page is read, and an LLM answers from the sources with "
            "numbered citations and links."
        ),
        "graph": WEB_RESEARCH,
        "requirements": [SEARCH_REQUIREMENT, LLM_REQUIREMENT],
        "triggers": [],
    },
    {
        "slug": "pdf-to-knowledge-base",
        "name": "PDF to Knowledge Base",
        "category": "Knowledge",
        "description": (
            "Upload a PDF, scan, or text file: it's read (OCR for scanned pages), split into chunks, embedded, and "
            "stored in the knowledge base named in the knowledge_base variable."
        ),
        "graph": PDF_TO_KB,
        "requirements": [EMBEDDING_REQUIREMENT],
        "triggers": [],
    },
    {
        "slug": "document-qa",
        "name": "Document Q&A",
        "category": "Knowledge",
        "description": (
            "Ask a question about your documents: the closest chunks are retrieved, an LLM reranks them, and the answer "
            "cites each source as [n] with its file and page."
        ),
        "graph": DOCUMENT_QA,
        "requirements": [EMBEDDING_REQUIREMENT, LLM_REQUIREMENT],
        "triggers": [],
    },
]


class TemplateRequirement(BaseModel):
    providers: list[str]
    label: str
    why: str | None = None
    satisfied: bool
    # The provider that would be used (the first with credentials), if any.
    using: str | None = None


class TemplateTrigger(BaseModel):
    type: TriggerType
    config: dict[str, Any]


class TemplateRead(BaseModel):
    slug: str
    name: str
    description: str | None
    category: str
    node_types: list[str] = Field(description="The node types in order, for the card's icons.")
    requirements: list[TemplateRequirement]
    ready: bool = Field(description="Every requirement is met with your (or the server's) credentials.")
    triggers: list[TemplateTrigger]


async def sync_templates(db: AsyncSession) -> int:
    """Write the catalog into the templates table (insert or update by slug)."""
    existing = {row.slug: row for row in await db.scalars(select(Template).where(Template.slug.is_not(None)))}
    for order, entry in enumerate(CATALOG):
        WorkflowGraph.model_validate(entry["graph"])  # the catalog itself must be well-formed
        row = existing.get(entry["slug"]) or Template(slug=entry["slug"])
        row.name, row.description, row.category = entry["name"], entry["description"], entry["category"]
        row.graph_json = entry["graph"]
        row.requirements_json = entry["requirements"]
        row.triggers_json = entry["triggers"]
        row.sort_order = order
        db.add(row)
    await db.commit()
    return len(CATALOG)


def _available(services: ExecutionServices, providers: list[str]) -> list[str]:
    real = services.settings.model_copy(update={"testing": False})
    # "local" (faster-whisper) needs no credential.
    return [name for name in providers if name == "local" or real.has_credentials(name)]


def _requirements(template: Template, services: ExecutionServices) -> list[TemplateRequirement]:
    result = []
    for entry in template.requirements_json or []:
        found = _available(services, entry["providers"])
        result.append(TemplateRequirement(
            providers=entry["providers"], label=entry["label"], why=entry.get("why"),
            satisfied=bool(found), using=found[0] if found else None,
        ))
    return result


async def list_templates(db: AsyncSession, user: User) -> list[TemplateRead]:
    services = await build_execution_services(db, user)
    rows = await db.scalars(select(Template).where(Template.slug.is_not(None)).order_by(Template.sort_order, Template.name))
    items = []
    for row in rows:
        requirements = _requirements(row, services)
        items.append(TemplateRead(
            slug=row.slug or "", name=row.name, description=row.description, category=row.category,
            node_types=[node["type"] for node in (row.graph_json or {}).get("nodes", [])],
            requirements=requirements, ready=all(r.satisfied for r in requirements),
            triggers=[TemplateTrigger.model_validate(t) for t in row.triggers_json or []],
        ))
    return items


def fit_graph(graph: dict[str, Any], services: ExecutionServices, *, email_to: str | None = None) -> dict[str, Any]:
    """The template's graph adapted to the user's credentials (see the module docstring).
    `email_to` is where a notification goes when Gmail is the only way to send it."""
    graph = copy.deepcopy(graph)
    llms = _available(services, list(FREE_LLMS))
    primary = llms[0] if llms else FREE_LLMS[0]
    fallback = llms[1:3]
    delivery = _available(services, ["telegram", "discord", "gmail"])
    # Keep Telegram when it's there (or nothing is: validation then says what's missing).
    notify = delivery[0] if delivery else "telegram"
    if notify == "gmail" and not email_to:
        notify = "telegram"
    has_groq = bool(_available(services, ["groq"]))
    for node in graph["nodes"]:
        if node["type"] == "speech_to_text" and not has_groq:
            node.setdefault("config", {})["provider"] = "local"
        subject = node.pop("email_subject", None)
        config = node.setdefault("config", {})
        if node["type"] in LLM_NODE_TYPES | LLM_STEP_TYPES:
            config["provider"] = primary
            config["fallback"] = fallback
            if node["type"] in LLM_NODE_TYPES:
                node["type"] = primary
        if node["type"] == "telegram" and notify == "discord":
            node["type"] = "discord_webhook"
            label = node.get("label") or ""
            node["label"] = label.replace("Telegram", "Discord") if "Telegram" in label else label or "Discord"
            node["config"] = {"content": config.get("text", "")}
        elif node["type"] == "telegram" and notify == "gmail":
            node["type"] = "gmail"
            label = node.get("label") or ""
            node["label"] = label.replace("Telegram", "email") if "Telegram" in label else label or "Email"
            node["config"] = {"to": email_to, "subject": subject or node["label"], "body": config.get("text", "")}
    return graph


async def _unique_name(db: AsyncSession, owner_id: uuid.UUID, name: str) -> str:
    taken = set(await db.scalars(select(Workflow.name).where(Workflow.owner_id == owner_id, Workflow.name.like(f"{name}%"))))
    if name not in taken:
        return name
    number = 2
    while f"{name} ({number})" in taken:
        number += 1
    return f"{name} ({number})"


class TemplateNotFound(LookupError):
    pass


async def ensure_default_kb(db: AsyncSession, user: User, services: ExecutionServices) -> None:
    """Create DEFAULT_KB for the knowledge templates if the user doesn't have it, embedded
    with the first real provider they have (mock word-matching without one)."""
    if await owned_kb(db, user.id, DEFAULT_KB) is not None:
        return
    provider = next(iter(_available(services, EMBEDDING_REQUIREMENT["providers"])), "mock")
    await create_kb(
        db, user.id, name=DEFAULT_KB, description="Created by a knowledge template.", embedding_provider=provider,
        embedding_model=None if provider == "mock" else services.settings.embedding_model(provider),
        chunk_size=DEFAULT_CHUNK_SIZE, chunk_overlap=DEFAULT_CHUNK_OVERLAP,
    )


async def use_template(db: AsyncSession, user: User, slug: str, *, timezone: str | None = None) -> Workflow:
    """A new workflow for `user` from the template, with its triggers created switched off."""
    template = await db.scalar(select(Template).where(Template.slug == slug))
    if template is None:
        raise TemplateNotFound(slug)
    services = await build_execution_services(db, user)
    graph = fit_graph(template.graph_json, services, email_to=user.email)
    if slug in SAMPLE_FILES:
        sample = await ensure_sample_file(db, user.id, SAMPLE_FILES[slug])
        for node in graph["nodes"]:
            if node["type"] == "input" and node["config"].get("input_type") == "file" and sample is not None:
                node["config"]["default"] = str(sample.id)
    if slug in KB_TEMPLATES:
        await ensure_default_kb(db, user, services)
    workflow = Workflow(
        name=await _unique_name(db, user.id, template.name),
        description=template.description,
        owner_id=user.id,
        status=WorkflowStatus.ACTIVE,
        graph_json=WorkflowGraph().model_dump(mode="json"),
    )
    db.add(workflow)
    await db.flush()
    await replace_graph(db, workflow, WorkflowGraph.model_validate(graph))
    workflow.version = 1
    tz = "UTC"
    if timezone:
        try:
            zone(timezone)
            tz = timezone
        except ScheduleError:
            logger.info("ignoring an unknown browser time zone", extra={"timezone": timezone})
    for entry in template.triggers_json or []:
        config = dict(entry.get("config") or {})
        if entry["type"] == TriggerType.SCHEDULE.value:
            config["timezone"] = tz
        db.add(WorkflowTrigger(workflow_id=workflow.id, type=TriggerType(entry["type"]), enabled=False, config_json=config))
    await db.commit()
    await db.refresh(workflow)
    logger.info("workflow created from a template", extra={"template": slug, "workflow_id": str(workflow.id)})
    return workflow
