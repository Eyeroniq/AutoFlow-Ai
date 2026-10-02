"""The resume refinement agents as drag-and-drop nodes: Resume Parse, ATS Check, Content Coach, Job Match, Resume Rewrite.

Each node is one agent of the multi-agent pipeline (flowforge_engine.nodes.resume_agents): its own prompt, JSON schema
and checks, one LLM call (with the usual provider fallback, one retry on a bad reply). They pass plain JSON along
the edges: Resume Parse produces `data` (the structured resume) that the others take as `parsed`, and Resume Rewrite
assembles the final draft with the guardrails (no invented numbers or claims, facts kept from the parsed resume).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from pydantic import Field

from flowforge_engine.files import FileNotAvailable, file_id_from
from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.nodes import resume_agents as agents
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

FREE_LLMS = ("gemini", "groq", "openrouter")


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


def provider_chain(context: NodeContext, provider: str) -> tuple[str, list[str]]:
    """`provider` (or, for "auto", the first free LLM that has credentials) first; the others are fallbacks."""
    services = context.services
    names = ["mock"] if services.settings.testing else [n for n in FREE_LLMS if services.has_credentials(n)]
    if provider != "auto":
        return provider, [n for n in names if n != provider]
    if not names:
        raise ValueError("No LLM is configured: add a Gemini, Groq, or OpenRouter key (Integrations or .env).")
    return names[0], names[1:]


class AgentConfig(NodeConfig):
    provider: str = Field(
        default="auto",
        description="'auto' uses your first free LLM (Gemini, Groq, OpenRouter) with the others as fallbacks.",
    )


def _result(run: agents.AgentRun, data: dict[str, Any] | None = None) -> NodeResult:
    if run.output is None:
        return NodeResult.fail(run.error or "the agent returned nothing usable")
    return NodeResult.ok(
        data=data if data is not None else run.output, warnings=run.warnings, provider_used=run.provider_used,
        model=run.model, duration_ms=run.duration_ms,
    )


def _parsed(value: Any) -> dict[str, Any] | str:
    """The parsed resume, or a message when `value` isn't the Resume Parse node's data."""
    if isinstance(value, dict) and isinstance(value.get("experience"), list):
        return value
    return "'parsed' must be the Resume Parse node's data, e.g. {{parse.data}}"


class ResumeParseConfig(AgentConfig):
    text: str = Field(min_length=1, description="The resume's text, e.g. {{read.text}} from a PDF Extract or OCR node.")


@register_node("resume_parse")
class ResumeParseNode(NodeDefinition[ResumeParseConfig]):
    category = "documents"
    label = "Resume Parse"
    description = "Parser agent: turns resume text into structured JSON (contact, summary, jobs with bullets, education, skills)."
    icon = "file-text"
    config_schema = ResumeParseConfig

    async def execute(self, context: NodeContext, config: ResumeParseConfig) -> NodeResult:
        try:
            provider, fallback = provider_chain(context, config.provider)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        text = agents.fit(config.text, agents.MAX_RESUME_CHARS)
        if len(text.strip()) < 80:
            return NodeResult.fail("The resume text is too short to parse; the PDF may be scanned (read it with an OCR node first).")
        run = await agents.run_agent(
            context, "parse", agents.render_prompt(agents.PARSER_PROMPT, resume_text=text), agents.PARSED_SCHEMA,
            provider=provider, fallback=fallback, check=lambda data: agents.check_parsed(data, text), temperature=0.0,
        )
        return _result(run)


class ResumeATSConfig(AgentConfig):
    parsed: Any = Field(description="The parsed resume, {{parse.data}}.")
    file: Any = Field(
        default=None,
        description="The resume PDF, e.g. {{resume.resume}}, for measured layout checks (columns, tables, headers). Optional.",
    )


@register_node("resume_ats")
class ResumeATSNode(NodeDefinition[ResumeATSConfig]):
    category = "documents"
    label = "ATS Check"
    description = "ATS agent: finds what applicant tracking systems would choke on, combining the model's review with measured PDF layout."
    icon = "scan-text"
    config_schema = ResumeATSConfig

    async def execute(self, context: NodeContext, config: ResumeATSConfig) -> NodeResult:
        from flowforge_engine.nodes.resume_layout import analyze_pdf, describe, layout_issues

        parsed = _parsed(config.parsed)
        if isinstance(parsed, str):
            return NodeResult.fail(parsed)
        layout: dict[str, Any] = {}
        if config.file:
            try:
                stored = await context.services.files.get(file_id_from(config.file))
            except (FileNotAvailable, ValueError) as exc:
                return NodeResult.fail(f"ATS Check: {exc}")
            layout = await asyncio.to_thread(analyze_pdf, str(stored.path))
        try:
            provider, fallback = provider_chain(context, config.provider)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        run = await agents.run_agent(
            context, "ats",
            agents.render_prompt(
                agents.ATS_PROMPT, layout=describe(layout) if layout else "(no PDF layout supplied)",
                headers=", ".join(layout.get("section_headers", [])) or "(none detected)", parsed=parsed,
            ),
            agents.ATS_SCHEMA, provider=provider, fallback=fallback, check=agents.check_ats,
        )
        if run.output is None:
            return _result(run)
        return _result(run, merge_ats_issues(run.output, layout_issues(layout) if layout else []))


class ResumeContentConfig(AgentConfig):
    parsed: Any = Field(description="The parsed resume, {{parse.data}}.")


@register_node("resume_content")
class ResumeContentNode(NodeDefinition[ResumeContentConfig]):
    category = "documents"
    label = "Content Coach"
    description = "Content agent: reviews every bullet (weak verbs, no metrics, vague, passive) and suggests rewrites without inventing facts."
    icon = "text-quote"
    config_schema = ResumeContentConfig

    async def execute(self, context: NodeContext, config: ResumeContentConfig) -> NodeResult:
        parsed = _parsed(config.parsed)
        if isinstance(parsed, str):
            return NodeResult.fail(parsed)
        try:
            provider, fallback = provider_chain(context, config.provider)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        bullets = agents.bullet_index(parsed)
        run = await agents.run_agent(
            context, "content", agents.render_prompt(agents.CONTENT_PROMPT, numbered=agents.numbered(parsed)),
            agents.CONTENT_SCHEMA, provider=provider, fallback=fallback, check=lambda data: agents.check_content(data, bullets),
        )
        if run.output is None:
            return _result(run)
        return _result(run, agents.sanitize_content(run.output, bullets))


class ResumeMatchConfig(AgentConfig):
    parsed: Any = Field(description="The parsed resume, {{parse.data}}.")
    job_description: str = Field(default="", description="The job posting text. Empty: the node is skipped (matched = false).")


@register_node("resume_match")
class ResumeMatchNode(NodeDefinition[ResumeMatchConfig]):
    category = "documents"
    label = "Job Match"
    description = "Job-match agent: compares the resume with a job description (keywords matched and missing, strongest bullets, score)."
    icon = "tags"
    config_schema = ResumeMatchConfig

    async def execute(self, context: NodeContext, config: ResumeMatchConfig) -> NodeResult:
        if not config.job_description.strip():
            return NodeResult.ok(data=None, matched=False, warnings=[], provider_used=None, model=None, duration_ms=0)
        parsed = _parsed(config.parsed)
        if isinstance(parsed, str):
            return NodeResult.fail(parsed)
        try:
            provider, fallback = provider_chain(context, config.provider)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        bullets = agents.bullet_index(parsed)
        run = await agents.run_agent(
            context, "job_match",
            agents.render_prompt(
                agents.JOB_MATCH_PROMPT, job_description=agents.fit(config.job_description, 16_000), numbered=agents.numbered(parsed),
            ),
            agents.JOB_MATCH_SCHEMA, provider=provider, fallback=fallback,
            check=lambda data: agents.check_job_match(data, bullets),
        )
        if run.output is None:
            return _result(run)
        result = _result(run, agents.sanitize_job_match(run.output, bullets))
        result.output["matched"] = True
        return result


class ResumeRewriteConfig(AgentConfig):
    parsed: Any = Field(description="The parsed resume, {{parse.data}}.")
    ats: Any = Field(description="The ATS Check's data, {{ats.data}}.")
    content: Any = Field(description="The Content Coach's data, {{content.data}}.")
    match: Any = Field(default=None, description="The Job Match's data, {{match.data}} (empty without a job description).")
    job_description: str = Field(default="", description="The job posting text, to tailor the rewrite. Optional.")


def _line(value: Any) -> str:
    return " | ".join(str(v) for v in value.values() if v) if isinstance(value, dict) else str(value)


def resume_to_text(resume: dict[str, Any]) -> str:
    """The refined resume as plain text (for an email or a Telegram message)."""
    contact = resume.get("contact_info") or {}
    lines = [str(contact.get("name") or "").strip(), " | ".join(str(v) for k, v in contact.items() if v and k != "name")]
    if resume.get("summary"):
        lines += ["", "SUMMARY", resume["summary"]]
    if resume.get("experience"):
        lines += ["", "EXPERIENCE"]
        for job in resume["experience"]:
            lines += [f"{job['title']}, {job['company']} ({job['dates']})", *[f"- {b}" for b in job["bullets"]], ""]
    if resume.get("education"):
        lines += ["EDUCATION", *[_line(e) for e in resume["education"]]]
    if resume.get("skills"):
        lines += ["", "SKILLS", ", ".join(map(str, resume["skills"]))]
    return "\n".join(lines).strip()


@register_node("resume_rewrite")
class ResumeRewriteNode(NodeDefinition[ResumeRewriteConfig]):
    category = "documents"
    label = "Resume Rewrite"
    description = (
        "Rewrite agent: drafts the improved resume from the reviews, then applies guardrails "
        "(no invented numbers or claims; facts kept from the original)."
    )
    icon = "braces"
    config_schema = ResumeRewriteConfig

    async def execute(self, context: NodeContext, config: ResumeRewriteConfig) -> NodeResult:
        parsed = _parsed(config.parsed)
        if isinstance(parsed, str):
            return NodeResult.fail(parsed)
        if not isinstance(config.ats, dict) or not isinstance(config.ats.get("issues"), list):
            return NodeResult.fail("'ats' must be the ATS Check node's data, e.g. {{ats.data}}")
        if not isinstance(config.content, dict) or not isinstance(config.content.get("bullets"), list):
            return NodeResult.fail("'content' must be the Content Coach node's data, e.g. {{content.data}}")
        match = config.match if isinstance(config.match, dict) and config.match else None
        try:
            provider, fallback = provider_chain(context, config.provider)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        job_section = ""
        if match is not None:
            job_section = (
                "JOB DESCRIPTION:\n" + config.job_description + "\n\nJOB-MATCH AGENT FINDINGS:\n" + agents.render_prompt("{x}", x=match)
            )
        run = await agents.run_agent(
            context, "rewrite",
            agents.render_prompt(
                agents.REWRITE_PROMPT, numbered=agents.numbered(parsed), content=config.content["bullets"],
                ats=[{k: i[k] for k in ("category", "severity", "title", "fix")} for i in config.ats["issues"]],
                job_section=job_section,
            ),
            agents.REWRITE_SCHEMA, provider=provider, fallback=fallback,
            check=lambda data: agents.check_rewrite(data, parsed, config.content), temperature=0.3,
        )
        if run.output is None:
            return _result(run)
        final = assemble(parsed, config.ats, config.content, match, run.output)
        return NodeResult.ok(
            resume=final["resume"], text=resume_to_text(final["resume"]), changes=final["changes"], stats=final["stats"],
            notes=final["notes"], corrections=final["corrections"], warnings=run.warnings,
            provider_used=run.provider_used, model=run.model, duration_ms=run.duration_ms,
        )
