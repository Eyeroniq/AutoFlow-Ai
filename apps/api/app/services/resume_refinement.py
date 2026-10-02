"""The multi-agent resume refinement pipeline: orchestration, storage, documents, and email.

    PDF -> extract (PDF Extract node + layout measurements)
        -> Parser Agent
        -> ATS Agent  | Content/Impact Agent | Job-Match Agent (only with a job description)   (concurrently)
        -> Rewrite Agent -> assembled result (rewritten draft, before/after per bullet, statistics)

Each agent is its own LLM call with its own role and schema (app.services.resume_agents), asked
through the engine's provider chain, so the usual fallback applies. Every stage, including each raw
model reply, is stored on the `resume_refinements` row as it finishes, so the UI shows the pipeline
progressing and can show the reasoning at each stage. Runs execute on a Celery worker
(app.worker.tasks.refine_resume_task); the API only creates the row and queues it.
"""

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from flowforge_engine import ExecutionServices, GraphNode, NodeContext, execute_node
from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.base import EmailAttachment, OutgoingEmail
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import SessionFactory
from app.models.file import UploadedFile
from app.models.resume import ResumeEmail, ResumeRefinement
from app.models.user import User
from app.services import resume_agents as agents
from app.services.credentials import build_execution_services
from app.services.files import file_path
from app.services.resume_documents import build_docx, build_pdf
from app.services.resume_layout import analyze_pdf, describe, layout_issues

logger = logging.getLogger(__name__)

STAGE_ORDER = ("extract", "parse", "ats", "content", "job_match", "rewrite")
STAGE_TITLES = {
    "extract": "Text and layout extraction",
    "parse": "Parser Agent",
    "ats": "ATS Compatibility Agent",
    "content": "Content & Impact Agent",
    "job_match": "Job-Match Agent",
    "rewrite": "Rewrite Agent",
}
STAGE_ROLES = {
    "extract": "Reads the PDF's text (PDF Extract node) and measures its layout: columns, tables, images, header/footer text.",
    "parse": "Turns the resume text into structured data: contact info, summary, experience, education, skills.",
    "ats": "Judges how safely an applicant tracking system can parse the resume, from the parsed structure and the measured layout.",
    "content": "Reviews every bullet for weak verbs, missing metrics, vague claims and passive voice, and writes a specific rewrite for each weak one.",
    "job_match": "Compares the resume with the job description: matched and missing keywords, and which bullets best answer which requirement.",
    "rewrite": "Writes the improved draft from everything above: same structure, every bullet rewritten per the feedback, ordered for the job.",
}
FREE_LLMS = ("gemini", "groq", "openrouter")
MAX_RAW_CHARS = 30_000
MAX_PROMPT_CHARS = 16_000


class RefinementError(Exception):
    """A request that can't be honoured; the message says why."""


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- Creating and reading ----------------------------------------------------------------------------


def initial_stages(has_jd: bool) -> dict[str, Any]:
    return {
        key: {
            "key": key, "title": STAGE_TITLES[key], "role": STAGE_ROLES[key],
            "status": "skipped" if key == "job_match" and not has_jd else "pending",
            **({"note": "No job description was given."} if key == "job_match" and not has_jd else {}),
        }
        for key in STAGE_ORDER
    }


async def create_refinement(db: AsyncSession, user: User, record: UploadedFile, job_description: str) -> ResumeRefinement:
    """A pending refinement of `record` (not yet queued). The same file refined before makes this the next version."""
    jd = job_description.strip()
    earlier = await db.scalar(
        select(func.count()).select_from(ResumeRefinement).where(
            ResumeRefinement.owner_id == user.id, ResumeRefinement.file_sha256 == record.sha256
        )
    )
    refinement = ResumeRefinement(
        owner_id=user.id, file_id=record.id, filename=record.filename, file_sha256=record.sha256,
        version=(earlier or 0) + 1, job_description=jd[: agents.MAX_JD_CHARS], status="pending", stages_json=initial_stages(bool(jd)),
    )
    db.add(refinement)
    await db.flush()
    return refinement


async def owned_refinement(db: AsyncSession, user_id: uuid.UUID, refinement_id: uuid.UUID) -> ResumeRefinement | None:
    return await db.scalar(
        select(ResumeRefinement).where(ResumeRefinement.id == refinement_id, ResumeRefinement.owner_id == user_id)
    )


# --- Running -----------------------------------------------------------------------------------------


def provider_chain(services: ExecutionServices) -> tuple[str, list[str]]:
    """The user's free LLMs in order (Gemini, Groq, OpenRouter): the first is asked, the rest are fallbacks."""
    names = ["mock"] if services.settings.testing else [n for n in FREE_LLMS if services.has_credentials(n)]
    if not names:
        raise RefinementError("No LLM is configured: add a Gemini, Groq, or OpenRouter key (Integrations or .env).")
    return names[0], names[1:]


def _stage_from_run(run: agents.AgentRun, base: dict[str, Any], started: datetime) -> dict[str, Any]:
    return {
        **base,
        "status": "success" if run.output is not None else "failed",
        "started_at": started.isoformat(), "finished_at": utcnow().isoformat(), "duration_ms": run.duration_ms,
        "provider_used": run.provider_used, "model": run.model, "mock": run.mock,
        "attempts": [{"raw": a["raw"][:MAX_RAW_CHARS], "problems": a["problems"]} for a in run.attempts],
        "fallback_errors": run.fallback_errors[:10],
        "warnings": run.warnings,
        "prompt": agents.fit(run.prompt, MAX_PROMPT_CHARS),
        "output": run.output, "error": run.error,
    }


class _Pipeline:
    def __init__(self, db: AsyncSession, refinement: ResumeRefinement, services: ExecutionServices):
        self.db, self.refinement, self.services = db, refinement, services
        self.context = NodeContext(workflow_id="resume-refinement", execution_id=str(refinement.id), services=services)
        self.provider, self.fallback = provider_chain(services)
        self.stages: dict[str, Any] = {k: dict(v) for k, v in refinement.stages_json.items()}
        self._lock = asyncio.Lock()
        self.has_jd = bool(refinement.job_description.strip())

    async def save(self, key: str, stage: dict[str, Any]) -> None:
        async with self._lock:
            self.stages[key] = stage
            self.refinement.stages_json = {k: dict(v) for k, v in self.stages.items()}
            await self.db.commit()

    async def begin(self, key: str) -> datetime:
        started = utcnow()
        await self.save(key, {**self.stages[key], "status": "running", "started_at": started.isoformat()})
        return started

    async def agent(self, key: str, prompt: str, schema: dict[str, Any], check: Any, *, temperature: float = 0.2) -> agents.AgentRun:
        started = await self.begin(key)
        run = await agents.run_agent(
            self.context, key, prompt, schema, provider=self.provider, fallback=self.fallback, check=check, temperature=temperature,
        )
        await self.save(key, _stage_from_run(run, self.stages[key], started))
        return run

    # -- stages ----------------------------------------------------------------------------------------

    async def extract(self) -> tuple[str, dict[str, Any]]:
        started = await self.begin("extract")
        record = await self.db.get(UploadedFile, self.refinement.file_id)
        if record is None:
            raise RefinementError("The uploaded PDF is gone.")
        context = NodeContext(workflow_id="resume-refinement", execution_id=str(self.refinement.id), services=self.services)
        result = await execute_node(
            GraphNode(id="extract", type="pdf_extract", config={"file": str(record.id)}), context, node_timeout=120,
        )
        if result.status.value != "success":
            raise RefinementError(f"Could not read the PDF: {result.error}")
        out = result.output or {}
        layout = await asyncio.to_thread(analyze_pdf, str(file_path(record)))
        text = out.get("text", "")
        stage = {
            **self.stages["extract"], "status": "success", "started_at": started.isoformat(), "finished_at": utcnow().isoformat(),
            "output": {
                "page_count": out.get("page_count"), "char_count": out.get("char_count"), "needs_ocr": out.get("needs_ocr"),
                "layout": layout, "text_preview": text[:1500],
            },
        }
        await self.save("extract", stage)
        if out.get("needs_ocr") or len(text.strip()) < 80:
            raise RefinementError(
                "This PDF has no selectable text (it looks scanned or image-only), which is exactly what an ATS can't read. "
                "Export the resume as a text-based PDF from Word or Google Docs and try again."
            )
        return text, layout

    async def run(self) -> dict[str, Any]:
        text, layout = await self.extract()
        text = agents.fit(text, agents.MAX_RESUME_CHARS)

        parse = await self.agent(
            "parse", agents.render_prompt(agents.PARSER_PROMPT, resume_text=text), agents.PARSED_SCHEMA,
            lambda data: agents.check_parsed(data, text), temperature=0.0,
        )
        if parse.output is None:
            raise RefinementError(f"Parser Agent failed: {parse.error}")
        parsed = parse.output
        bullets = agents.bullet_index(parsed)
        numbered = agents.numbered(parsed)

        async def ats() -> agents.AgentRun:
            run = await self.agent(
                "ats",
                agents.render_prompt(
                    agents.ATS_PROMPT, layout=describe(layout), headers=", ".join(layout.get("section_headers", [])) or "(none detected)",
                    parsed=parsed,
                ),
                agents.ATS_SCHEMA, agents.check_ats,
            )
            if run.output is not None:
                merged = merge_ats_issues(run.output, layout_issues(layout))
                await self.save("ats", {**self.stages["ats"], "output": merged})
            return run

        async def content() -> agents.AgentRun:
            run = await self.agent(
                "content", agents.render_prompt(agents.CONTENT_PROMPT, numbered=numbered), agents.CONTENT_SCHEMA,
                lambda data: agents.check_content(data, bullets),
            )
            if run.output is not None:
                run.output = agents.sanitize_content(run.output, bullets)
                await self.save("content", {**self.stages["content"], "output": run.output})
            return run

        async def job_match() -> agents.AgentRun | None:
            if not self.has_jd:
                return None
            run = await self.agent(
                "job_match",
                agents.render_prompt(agents.JOB_MATCH_PROMPT, job_description=self.refinement.job_description, numbered=numbered),
                agents.JOB_MATCH_SCHEMA, lambda data: agents.check_job_match(data, bullets),
            )
            if run.output is not None:
                run.output = agents.sanitize_job_match(run.output, bullets)
                await self.save("job_match", {**self.stages["job_match"], "output": run.output})
            return run

        ats_run, content_run, match_run = await asyncio.gather(ats(), content(), job_match())
        for run in (ats_run, content_run, match_run):
            if run is not None and run.output is None:
                raise RefinementError(f"{STAGE_TITLES[run.key]} failed: {run.error}")
        ats_output = self.stages["ats"]["output"]

        job_section = ""
        if match_run is not None:
            job_section = ("JOB DESCRIPTION:\n" + self.refinement.job_description
                           + "\n\nJOB-MATCH AGENT FINDINGS:\n" + agents.render_prompt("{x}", x=match_run.output))
        rewrite = await self.agent(
            "rewrite",
            agents.render_prompt(
                agents.REWRITE_PROMPT, numbered=numbered, content=content_run.output["bullets"],
                ats=[{k: i[k] for k in ("category", "severity", "title", "fix")} for i in ats_output["issues"]], job_section=job_section,
            ),
            agents.REWRITE_SCHEMA, lambda data: agents.check_rewrite(data, parsed, content_run.output), temperature=0.3,
        )
        if rewrite.output is None:
            raise RefinementError(f"Rewrite Agent failed: {rewrite.error}")
        return assemble(
            parsed, ats_output, content_run.output, match_run.output if match_run else None, rewrite.output,
        )


def merge_ats_issues(output: dict[str, Any], measured: list[dict[str, Any]]) -> dict[str, Any]:
    """The agent's issues, plus any measured finding in a category the agent didn't report: a blocker the
    model overlooked still reaches the report. Each issue says where it came from."""
    issues = [{**i, "source": "agent"} for i in output["issues"]]
    reported = {i["category"] for i in issues}
    issues += [{**m, "source": "layout"} for m in measured if m["category"] not in reported]
    issues.sort(key=lambda i: (i["severity"] != "blocker",))
    return {**output, "issues": issues}


def assemble(
    parsed: dict[str, Any], ats: dict[str, Any], content: dict[str, Any], match: dict[str, Any] | None,
    rewrite: dict[str, Any],
) -> dict[str, Any]:
    """The final result from the agents' outputs. Facts the agents must not change (contact details,
    companies, titles, dates, education) are taken from the parsed resume, whatever the Rewrite Agent returned."""
    index = agents.bullet_index(parsed)
    feedback = {b["id"]: b for b in content["bullets"]}
    matched_by: dict[str, list[str]] = {}
    for req in (match or {}).get("requirement_matches", []):
        for bid in req["bullet_ids"]:
            matched_by.setdefault(bid, []).append(req["requirement"])
    experience, changes, corrections = [], [], []
    for i, (job, new) in enumerate(zip(parsed["experience"], rewrite["experience"], strict=True)):
        texts = []
        # Without a job description there is nothing to reorder for: keep each job's original order.
        ordered = new["bullets"] if match is not None else sorted(new["bullets"], key=lambda b: int(b["source_id"].split(".b")[1]))
        for position, bullet in enumerate(ordered):
            sid = bullet["source_id"]
            before = index[sid]["text"]
            after = bullet["text"].strip()
            note = feedback.get(sid, {"flags": [], "explanation": ""})
            # Guardrails the models can't be trusted with: no invented numbers, the right tense, and untouched
            # bullets staying untouched unless a job description asks for tailoring. Fall back to the Content
            # Agent's suggestion if it is clean, else to the original, and say so.
            suggestion = (note.get("rewrite") or "").strip()
            clean_suggestion = suggestion if suggestion and not agents.invented_numbers(suggestion, before) else ""
            reason = None
            if after != before and (made_up := agents.invented_numbers(after, before, suggestion)):
                after, reason = clean_suggestion or before, f"removed invented number(s) {sorted(made_up)}"
            elif after != before and (padding := agents.padded(after, before, suggestion)):
                after, reason = clean_suggestion or before, f"removed invented claims ({', '.join(padding[:5])})"
            elif after != before and agents.wrong_tense(after, index[sid]["current"]):
                after, reason = clean_suggestion or before, "past-role bullet didn't start with a past-tense verb"
            elif after != before and not note["flags"] and match is None:
                after, reason = before, "the Content Agent found nothing to fix, so it stays as written"
            if reason:
                corrections.append({"source_id": sid, "reason": reason})
            original_position = int(sid.split(".b")[1])
            changed = after.strip() != before.strip()
            moved = position != original_position
            changes.append({
                "source_id": sid, "job": i, "company": job["company"], "title": job["title"],
                "before": before, "after": after, "changed": changed, "moved": moved,
                "flags": note["flags"], "explanation": note["explanation"], "agent_rewrite": note.get("rewrite", ""),
                "job_requirements": matched_by.get(sid, []),
            })
            texts.append(after)
        experience.append({"company": job["company"], "title": job["title"], "dates": job["dates"], "bullets": texts})
    summary = rewrite["summary"].strip()
    if summary and agents.invented_numbers(summary, agents.resume_text(parsed)):
        corrections.append({"source_id": "summary", "reason": "the new summary contained numbers that aren't in the resume; kept the original"})
        summary = ""
    resume = {
        "contact_info": parsed["contact_info"],
        "summary": summary or parsed["summary"],
        "experience": experience,
        "education": parsed["education"],
        "skills": rewrite["skills"] or parsed["skills"],
    }
    placeholders = sum(len(re.findall(r"\[[^\]\n]{1,40}\]", c["after"])) for c in changes if "[" not in c["before"])
    issues = ats["issues"]
    stats: dict[str, Any] = {
        "bullets_total": len(index),
        "bullets_rewritten": sum(1 for c in changes if c["changed"]),
        "bullets_reordered": sum(1 for c in changes if c["moved"]),
        "bullets_flagged": sum(1 for b in content["bullets"] if b["flags"]),
        "flag_counts": {f: sum(1 for b in content["bullets"] if f in b["flags"]) for f in agents.BULLET_FLAGS},
        "ats_score": ats["score"],
        "ats_blockers": sum(1 for i in issues if i["severity"] == "blocker"),
        "ats_warnings": sum(1 for i in issues if i["severity"] == "warning"),
        "ats_fixed_by_reformat": sum(1 for i in issues if i["category"] in agents.FIXED_BY_REFORMAT),
        "ats_remaining": [i["title"] for i in issues if i["category"] not in agents.FIXED_BY_REFORMAT],
        "placeholders": placeholders,
        "summary_changed": resume["summary"].strip() != parsed["summary"].strip(),
    }
    if match is not None:
        matched, missing = len(match["matched_keywords"]), len(match["missing_keywords"])
        stats.update({
            "jd": True, "keywords_matched": matched, "keywords_total": matched + missing, "match_score": match["match_score"],
            "missing_high": [k["keyword"] for k in match["missing_keywords"] if k["importance"] == "high"],
        })
    else:
        stats["jd"] = False
    stats["corrections"] = len(corrections)
    return {
        "resume": resume, "changes": changes, "stats": stats, "notes": rewrite["notes"], "corrections": corrections,
        "summary_review": content.get("summary_review", ""), "original_summary": parsed["summary"],
    }


async def run_refinement(session_factory: SessionFactory, refinement_id: uuid.UUID) -> dict[str, Any]:
    """The worker's entry point: run the pipeline for one refinement; it ends `success` or `failed`."""
    async with session_factory() as db:
        refinement = await db.get(ResumeRefinement, refinement_id)
        if refinement is None or refinement.status != "pending":
            return {"status": "skipped"}
        refinement.status, refinement.started_at = "running", utcnow()
        await db.commit()
        try:
            owner = await db.get(User, refinement.owner_id)
            services = await build_execution_services(db, owner)
            result = await _Pipeline(db, refinement, services).run()
            refinement.result_json, refinement.status = result, "success"
        except RefinementError as exc:
            refinement.status, refinement.error_message = "failed", str(exc)
        except Exception as exc:  # the row must never stay "running"
            logger.exception("resume refinement crashed", extra={"refinement_id": str(refinement_id)})
            refinement.status, refinement.error_message = "failed", f"Unexpected error: {type(exc).__name__}: {exc}"
        refinement.finished_at = utcnow()
        await db.commit()
        return {"status": refinement.status}


async def fail_stale(session_factory: SessionFactory, *, older_than_seconds: float = 3600) -> int:
    """Refinements stuck running (a worker died) are marked failed, so the UI stops waiting."""
    cutoff = utcnow().timestamp() - older_than_seconds
    count = 0
    async with session_factory() as db:
        rows = await db.scalars(select(ResumeRefinement).where(ResumeRefinement.status.in_(("pending", "running"))))
        for row in rows:
            if row.updated_at.timestamp() < cutoff:
                row.status, row.error_message, row.finished_at = "failed", "The run was interrupted before it finished.", utcnow()
                count += 1
        await db.commit()
    return count


# --- Documents and email -------------------------------------------------------------------------------


def documents(refinement: ResumeRefinement) -> dict[str, tuple[bytes, str]]:
    """{"pdf": (bytes, content type), "docx": (...)} of the refined draft."""
    if refinement.status != "success" or not refinement.result_json:
        raise RefinementError("The refinement hasn't finished successfully.")
    resume = refinement.result_json["resume"]
    return {
        "pdf": (build_pdf(resume), "application/pdf"),
        "docx": (build_docx(resume), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    }


def document_name(refinement: ResumeRefinement, extension: str) -> str:
    stem = re.sub(r"\.pdf$", "", refinement.filename, flags=re.I)
    stem = re.sub(r"[^\w.-]+", "-", stem).strip("-") or "resume"
    return f"{stem}-refined-v{refinement.version}.{extension}"


def email_summary(refinement: ResumeRefinement) -> str:
    """What changed, in a sentence or two, from the stored statistics."""
    s = refinement.result_json["stats"]
    parts = [f"Rewrote {s['bullets_rewritten']} of {s['bullets_total']} bullets"]
    if s["ats_fixed_by_reformat"]:
        parts.append(f"fixed {s['ats_fixed_by_reformat']} ATS formatting issue{'s' if s['ats_fixed_by_reformat'] != 1 else ''} "
                     "(the attached files are single-column, plain text)")
    if s.get("jd"):
        parts.append(f"matched {s['keywords_matched']} of {s['keywords_total']} job-description keywords")
    text = ", ".join(parts) + "."
    if s["ats_remaining"]:
        text += f" Still for you to look at: {'; '.join(s['ats_remaining'][:3])}."
    if s["placeholders"]:
        text += (f" {s['placeholders']} placeholder{'s' if s['placeholders'] != 1 else ''} in square brackets (like [X%]) "
                 "need your real numbers before you send this anywhere.")
    return text


class EmailUnavailable(Exception):
    """Sending isn't possible right now (no email account is connected)."""


async def email_refinement(
    db: AsyncSession, user: User, refinement: ResumeRefinement, services: ExecutionServices
) -> ResumeEmail:
    """Email the refined resume (PDF and DOCX) to the account's own address, and record it. The recipient
    is never a parameter: an auto-rewritten resume goes to its owner for review, not to anyone else."""
    import base64

    files = documents(refinement)
    if not services.has_credentials("gmail") and not services.settings.testing:
        raise EmailUnavailable("No email account is connected. Add Gmail (SMTP) credentials under Integrations or in .env.")
    summary = email_summary(refinement)
    subject = f"Your refined resume (version {refinement.version}): {refinement.filename}"
    body = (
        f"Hi {user.full_name.split()[0] if user.full_name.strip() else 'there'},\n\n"
        f"Your resume \"{refinement.filename}\" has been refined (version {refinement.version}). {summary}\n\n"
        "Attached: a PDF and an editable Word (DOCX) copy of the new draft. Please read it through before you use it: "
        "the rewrite is a draft that keeps your facts and suggests stronger wording, and you know your work best.\n\n"
        "FlowForge AI"
    )
    attachments = [
        EmailAttachment(
            filename=document_name(refinement, ext), content=base64.b64encode(data).decode(), encoding="base64", content_type=ctype
        )
        for ext, (data, ctype) in files.items()
    ]
    email = OutgoingEmail(to=[user.email], subject=subject, body=body, attachments=attachments)
    try:
        receipt = await services.email("gmail").send_email(email)
    except ProviderError as exc:
        raise EmailUnavailable(str(exc)) from exc
    row = ResumeEmail(
        refinement_id=refinement.id, user_id=user.id, to_email=user.email, version=refinement.version, subject=subject,
        summary=summary, message_id=str(receipt.get("message_id") or "")[:255] or None,
        attachments_json={"files": [{"filename": a.filename, "bytes": len(a.data())} for a in attachments], "status": receipt.get("status")},
    )
    db.add(row)
    await db.flush()
    return row


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
