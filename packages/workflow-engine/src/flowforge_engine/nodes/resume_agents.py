"""The agents of the resume refinement pipeline: what each is asked, the JSON each must return, and
the checks a reply must pass beyond its schema.

Five agents, each one LLM call with its own role (never one mega-prompt):

  parse      Parser Agent          resume text -> {contact_info, summary, experience, education, skills}
  ats        ATS Agent             parsed resume + measured PDF layout -> issues (blocker / warning)
  content    Content/Impact Agent  every bullet -> flags (weak verb, no metric, vague, passive) + a rewrite
  job_match  Job-Match Agent       parsed resume + job description -> keywords, gaps, strongest bullets
  rewrite    Rewrite Agent         everything above -> the improved draft, bullet by bullet

`run_agent` asks through the engine's provider chain (flowforge_engine.nodes.ai.generate_with_fallback,
so the usual fallback applies), validates the reply against the agent's schema and its extra checks,
retries once with the problems listed, and keeps every raw reply so the UI can show each stage.
"""

import copy
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from flowforge_engine.models import NodeContext
from flowforge_engine.jsonschema_lite import validate
from flowforge_engine.nodes.ai import LLMChainFailed, generate_with_fallback
from flowforge_engine.nodes.structured import check_reply, retry_request, schema_request

MAX_ATTEMPTS = 3
MAX_RESUME_CHARS = 40_000
MAX_JD_CHARS = 12_000

STANDARD_SECTIONS = ("summary", "experience", "education", "skills")
ATS_CATEGORIES = (
    "multi_column", "table", "image_or_icon", "header_footer", "nonstandard_header", "missing_section",
    "contact_info", "date_format", "special_characters", "length", "other",
)
# Categories a regenerated single-column, text-only document fixes by itself.
FIXED_BY_REFORMAT = ("multi_column", "table", "image_or_icon", "header_footer", "nonstandard_header", "special_characters")
BULLET_FLAGS = ("weak_opening_verb", "no_metric", "vague_claim", "passive_voice")


def _str(description: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra} if description else {"type": "string", **extra}


def _list(items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "array", "items": items, **extra}


# --- Schemas ----------------------------------------------------------------------------------

CONTACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "email", "phone", "location", "links"],
    "properties": {
        "name": _str(), "email": _str(), "phone": _str(), "location": _str(),
        "links": _list(_str(), description="LinkedIn, GitHub, portfolio, website URLs exactly as written"),
    },
}

EDUCATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["institution", "degree", "dates", "details"],
    "properties": {"institution": _str(), "degree": _str(), "dates": _str(), "details": _str("GPA, honors, coursework: as written, or empty")},
}

PARSED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["contact_info", "summary", "experience", "education", "skills"],
    "properties": {
        "contact_info": CONTACT_SCHEMA,
        "summary": _str("The professional summary / objective paragraph as written, or empty"),
        "experience": _list({
            "type": "object",
            "required": ["company", "title", "dates", "bullets"],
            "properties": {
                "company": _str(), "title": _str(), "dates": _str("As written, e.g. 'Jan 2021 - Present'"),
                "bullets": _list(_str(minLength=1), description="One entry per bullet point, text only, no bullet character"),
            },
        }),
        "education": _list(EDUCATION_SCHEMA),
        "skills": _list(_str(minLength=1), description="Individual skills, not categories"),
    },
}

ATS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["score", "summary", "issues"],
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "How safely an ATS will parse this resume"},
        "summary": _str("Two sentences: the overall ATS picture"),
        "issues": _list({
            "type": "object",
            "required": ["category", "severity", "title", "explanation", "fix"],
            "properties": {
                "category": {"type": "string", "enum": list(ATS_CATEGORIES)},
                "severity": {"type": "string", "enum": ["blocker", "warning"]},
                "title": _str(minLength=3),
                "explanation": _str("Plain language: what is wrong and why an ATS or recruiter is hurt by it", minLength=20),
                "evidence": _str("What was measured or seen (page, section, numbers)"),
                "fix": _str("The concrete change to make", minLength=5),
            },
        }),
    },
}

CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["overall", "summary_review", "bullets"],
    "properties": {
        "overall": _str("Two or three sentences on the resume's content strength: the pattern you saw across bullets"),
        "summary_review": _str("One or two sentences on the professional summary: clichés, vagueness, what it should say instead. Empty if the resume has no summary"),
        "bullets": _list({
            "type": "object",
            "required": ["id", "flags", "explanation", "rewrite"],
            "properties": {
                "id": _str("The bullet id exactly as given, e.g. e0.b2"),
                "flags": _list({"type": "string", "enum": list(BULLET_FLAGS)}),
                "explanation": _str("What is wrong with this specific bullet, or empty when it is fine"),
                "rewrite": _str("A specific rewrite of this bullet when flagged; empty when not flagged"),
            },
        }),
    },
}

JOB_MATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["match_score", "summary", "matched_keywords", "missing_keywords", "requirement_matches"],
    "properties": {
        "match_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": _str("Two sentences: how well the resume fits this job and the biggest gap"),
        "matched_keywords": _list({
            "type": "object", "required": ["keyword", "evidence"],
            "properties": {"keyword": _str(minLength=1), "evidence": _str("Where the resume shows it")},
        }),
        "missing_keywords": _list({
            "type": "object", "required": ["keyword", "importance", "suggestion"],
            "properties": {
                "keyword": _str(minLength=1),
                "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                "suggestion": _str("How to add it honestly: which existing experience could carry it, or that it is a true gap"),
            },
        }),
        "requirement_matches": _list({
            "type": "object", "required": ["requirement", "strength", "bullet_ids", "note"],
            "properties": {
                "requirement": _str("A requirement taken from the job description", minLength=3),
                "strength": {"type": "string", "enum": ["strong", "partial", "weak", "none"]},
                "bullet_ids": _list(_str(), description="Ids of the resume's strongest bullets for this requirement (empty if none)"),
                "note": _str(),
            },
        }),
    },
}

REWRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "experience", "skills", "notes"],
    "properties": {
        "summary": _str("The improved professional summary (may be empty only if the original had none and none is warranted)"),
        "experience": _list({
            "type": "object",
            "required": ["company", "title", "dates", "bullets"],
            "properties": {
                "company": _str(), "title": _str(), "dates": _str(),
                "bullets": _list({
                    "type": "object", "required": ["text", "source_id"],
                    "properties": {
                        "text": _str(minLength=3),
                        "source_id": _str("Id of the original bullet this one rewrites, e.g. e0.b2"),
                    },
                }),
            },
        }),
        "skills": _list(_str(minLength=1)),
        "notes": _str("What you changed and why, in two or three sentences"),
    },
}


# --- Helpers on the parsed resume ------------------------------------------------------------------


def bullet_index(parsed: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Every experience bullet by id ("e<job>.b<bullet>"), in resume order."""
    found: dict[str, dict[str, str]] = {}
    for i, job in enumerate(parsed.get("experience", [])):
        for j, text in enumerate(job.get("bullets", [])):
            found[f"e{i}.b{j}"] = {
                "text": text, "company": job.get("company", ""), "title": job.get("title", ""), "current": is_current(job),
            }
    return found


CURRENT_WORDS = re.compile(r"\b(present|current|now|ongoing|today|till date|to date)\b", re.I)


def is_current(job: dict[str, Any]) -> bool:
    """A role that is still held (its dates say Present or similar): its bullets are in the present tense."""
    return bool(CURRENT_WORDS.search(str(job.get("dates", ""))))


def resume_text(parsed: dict[str, Any]) -> str:
    """The parsed resume flattened, for substring checks."""
    parts = [parsed.get("summary", ""), *parsed.get("skills", [])]
    for job in parsed.get("experience", []):
        parts += [job.get("company", ""), job.get("title", ""), *job.get("bullets", [])]
    for school in parsed.get("education", []):
        parts += [school.get("institution", ""), school.get("degree", ""), school.get("details", "")]
    return "\n".join(str(p) for p in parts)


def numbered(parsed: dict[str, Any]) -> str:
    """The resume with bullet ids, the form the agents are shown."""
    lines = []
    if parsed.get("summary"):
        lines += ["SUMMARY", parsed["summary"], ""]
    lines.append("EXPERIENCE")
    for i, job in enumerate(parsed.get("experience", [])):
        tense = "CURRENT role: present tense" if is_current(job) else "PAST role: past tense"
        lines.append(f"[job {i}] {job.get('title', '')} at {job.get('company', '')} ({job.get('dates', '')}) <{tense}>")
        lines += [f"  {bid}: {text}" for j, text in enumerate(job.get("bullets", [])) if (bid := f"e{i}.b{j}")]
    lines += ["", "EDUCATION"]
    lines += [f"  {s.get('degree', '')}, {s.get('institution', '')} ({s.get('dates', '')}) {s.get('details', '')}".rstrip()
              for s in parsed.get("education", [])]
    lines += ["", "SKILLS", ", ".join(parsed.get("skills", []))]
    return "\n".join(lines)


# --- Extra checks (what a schema can't say) --------------------------------------------------------------

# A problem starting with "~" is soft: it makes the model retry once, but if the retry still has it the
# output is accepted (and the problem is kept as a warning); the assembler then corrects it deterministically.
SOFT = "~"
IRREGULAR_PAST = frozenset({
    "led", "built", "ran", "wrote", "drove", "grew", "won", "cut", "set", "sold", "spent", "taught", "oversaw", "chose",
    "made", "took", "gave", "got", "began", "brought", "bought", "caught", "found", "held", "kept", "left", "met", "paid",
    "put", "read", "saw", "sent", "spoke", "stood", "thought", "understood", "undertook", "became", "rebuilt", "forged",
})
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
BRACKETED = re.compile(r"\[[^\]]*\]")


def numbers_in(text: str) -> set[str]:
    """The numbers written in `text` (commas dropped), ignoring [placeholders]."""
    return {n.replace(",", "") for n in NUMBER.findall(BRACKETED.sub(" ", text))}


def invented_numbers(after: str, *known: str) -> set[str]:
    """Numbers in `after` that appear in none of the `known` texts: the model made them up."""
    allowed: set[str] = set()
    for text in known:
        allowed |= numbers_in(text)
    return numbers_in(after) - allowed


STOPWORDS = frozenset(
    "the and for with that this from into over under across through their its our your about also while when than then "
    "them they have has had been being were was are will would could should which what who whom whose each every both such "
    "more most very".split()
)
# How many content words a rewrite may add beyond the original (a stronger verb, a few connecting words).
MAX_ADDED_WORDS = 4


def added_words(after: str, *known: str) -> list[str]:
    """Content words in `after` that none of the `known` texts contain: what the rewrite added. Placeholders
    and short or common words don't count, and neither do words that merely change ending (manage/managed)."""
    seen = {w for text in known for w in re.findall(r"[a-z0-9']+", text.lower())}
    stems = {w[:5] for w in seen}
    new = []
    for word in re.findall(r"[a-z0-9']+", BRACKETED.sub(" ", after).lower()):
        if len(word) > 3 and word not in STOPWORDS and word not in seen and word[:5] not in stems:
            new.append(word)
    return new


def same_text(a: str, b: str) -> bool:
    """Equal ignoring case, punctuation and spacing: a rewrite that only moves a full stop isn't a rewrite."""
    return re.sub(r"\W+", "", a).lower() == re.sub(r"\W+", "", b).lower()


def has_figure(text: str) -> bool:
    """A number or a [placeholder] for one."""
    return bool(NUMBER.search(text) or BRACKETED.search(text))


def padded(after: str, *known: str) -> list[str]:
    """The added words when the rewrite adds more than a stronger verb and a few connectors: invented clauses."""
    new = added_words(after, *known)
    return new if len(new) > MAX_ADDED_WORDS else []


def wrong_tense(text: str, current: bool) -> bool:
    """A bullet of a past role that doesn't open with a past-tense verb ("Develop REST APIs")."""
    if current:
        return False
    words = text.strip().split()
    first = re.sub(r"\W", "", words[0]).lower() if words else ""
    return bool(first) and not (first.endswith("ed") or first in IRREGULAR_PAST)


def check_parsed(parsed: dict[str, Any], source_text: str = "") -> list[str]:
    """Problems with a parsed resume beyond its schema: structure that would mislead the later agents."""
    problems: list[str] = []
    contact = parsed.get("contact_info", {})
    if not (contact.get("name") or "").strip():
        problems.append("contact_info.name is empty: take the candidate's name from the top of the resume")
    if not parsed.get("experience") and not parsed.get("education") and not parsed.get("skills"):
        problems.append("experience, education and skills are all empty: the resume text has content to extract")
    for i, job in enumerate(parsed.get("experience", [])):
        if not (job.get("company") or "").strip() and not (job.get("title") or "").strip():
            problems.append(f"experience[{i}] has neither company nor title")
        if not job.get("bullets"):
            problems.append(f"experience[{i}] ({job.get('company') or job.get('title')}) has no bullets; copy its bullet points")
    if source_text:
        # The parser may restructure, never invent: every bullet must come from the resume text.
        squashed = re.sub(r"\W+", "", source_text).lower()
        missing = [
            text for job in parsed.get("experience", []) for text in job.get("bullets", [])
            if len(text) > 25 and re.sub(r"\W+", "", text).lower()[:40] not in squashed
        ]
        if missing:
            problems.append(f"{len(missing)} bullet(s) don't appear in the resume text (first: {missing[0][:60]!r}); copy bullets verbatim")
    return problems


def check_ats(data: dict[str, Any]) -> list[str]:
    problems = []
    for i, issue in enumerate(data.get("issues", [])):
        if issue["category"] == "missing_section" and not any(s in issue["title"].lower() + issue["explanation"].lower() for s in STANDARD_SECTIONS):
            problems.append(f"issues[{i}] is missing_section but doesn't name which section")
    return problems


def check_content(data: dict[str, Any], bullets: dict[str, dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(data.get("bullets", [])):
        bid = item["id"]
        if bid not in bullets:
            problems.append(f"{SOFT}bullets[{i}].id '{bid}' isn't one of the given bullet ids")
            continue
        if bid in seen:
            problems.append(f"{SOFT}bullet {bid} is reviewed twice")
        seen.add(bid)
        flagged = bool(item["flags"])
        rewrite = item["rewrite"].strip()
        if flagged and not rewrite:
            problems.append(f"{bid} is flagged {item['flags']} but has no rewrite")
        if flagged and not item["explanation"].strip():
            problems.append(f"{bid} is flagged but has no explanation")
        if flagged and same_text(rewrite, bullets[bid]["text"]):
            problems.append(f"{bid}'s rewrite is identical to the original; change the wording, don't just move the punctuation")
        if flagged and rewrite and "no_metric" in item["flags"] and not has_figure(rewrite):
            problems.append(f"{SOFT}{bid} is flagged no_metric but its rewrite has no number and no [placeholder] such as [X%] or [N users]")
        if not flagged and rewrite:
            problems.append(f"{bid} has a rewrite but no flags: flag it or leave the rewrite empty")
        if flagged and rewrite:
            if made_up := invented_numbers(rewrite, bullets[bid]["text"]):
                problems.append(f"{SOFT}{bid}'s rewrite invents the number(s) {sorted(made_up)}: use a [placeholder] such as [X%] instead")
            if extra := padded(rewrite, bullets[bid]["text"]):
                problems.append(f"{SOFT}{bid}'s rewrite adds claims the original doesn't make ({', '.join(extra[:6])}): drop those clauses, but keep the stronger verb and the [placeholder] for the missing number")
            if wrong_tense(rewrite, bullets[bid]["current"]):
                problems.append(f"{SOFT}{bid} belongs to a PAST role: its rewrite must open with a past-tense verb (Developed, Led), not '{rewrite.split()[0]}'")
    missing = [bid for bid in bullets if bid not in seen]
    if missing:
        problems.append(f"these bullets were not reviewed: {', '.join(missing[:12])}")
    return problems


def check_job_match(data: dict[str, Any], bullets: dict[str, dict[str, str]]) -> list[str]:
    problems = []
    for i, match in enumerate(data.get("requirement_matches", [])):
        unknown = [b for b in match["bullet_ids"] if b not in bullets]
        if unknown:
            problems.append(f"{SOFT}requirement_matches[{i}] cites unknown bullet ids: {', '.join(unknown)}")
        if match["strength"] != "none" and not match["bullet_ids"] and len(match["note"].strip()) < 10:
            problems.append(f"requirement_matches[{i}] has strength '{match['strength']}' but no bullet ids and no note saying what supports it")
    if not data.get("matched_keywords") and not data.get("missing_keywords"):
        problems.append("no keywords at all: list the job description's keywords as matched or missing")
    return problems


def check_rewrite(data: dict[str, Any], parsed: dict[str, Any], content: dict[str, Any] | None = None) -> list[str]:
    """The rewrite keeps the structure: the same jobs in the same order, every original bullet used
    exactly once (reordering inside a job is allowed), and no skill the resume doesn't already show."""
    problems: list[str] = []
    jobs = parsed.get("experience", [])
    if len(data.get("experience", [])) != len(jobs):
        problems.append(f"experience must have {len(jobs)} entries (one per original job, same order), got {len(data.get('experience', []))}")
        return problems
    index = bullet_index(parsed)
    used: list[str] = []
    for i, job in enumerate(data["experience"]):
        for j, bullet in enumerate(job["bullets"]):
            source = bullet["source_id"]
            if source not in index:
                problems.append(f"experience[{i}].bullets[{j}].source_id '{source}' is unknown")
            elif not source.startswith(f"e{i}."):
                problems.append(f"experience[{i}].bullets[{j}] rewrites {source}, which belongs to another job")
            used.append(source)
    for bid in index:
        if used.count(bid) != 1:
            problems.append(f"original bullet {bid} must appear exactly once (found {used.count(bid)})")
    suggested = {b["id"]: b["rewrite"] for b in (content or {}).get("bullets", [])}
    for i, job in enumerate(data["experience"]):
        for j, bullet in enumerate(job["bullets"]):
            source = bullet["source_id"]
            if source not in index:
                continue
            made_up = invented_numbers(bullet["text"], index[source]["text"], suggested.get(source, ""))
            extra = padded(bullet["text"], index[source]["text"], suggested.get(source, ""))
            if extra:
                problems.append(f"{SOFT}{source}: the rewrite adds claims neither the original nor the suggestion makes ({', '.join(extra[:6])}); cut them")
            if made_up:
                problems.append(f"{SOFT}{source}: the rewrite invents the number(s) {sorted(made_up)}; keep the original's numbers or use a [placeholder]")
            elif wrong_tense(bullet["text"], index[source]["current"]):
                problems.append(f"{SOFT}{source} belongs to a PAST role: open it with a past-tense verb, not '{bullet['text'].split()[0]}'")
    if made_up := invented_numbers(data.get("summary", ""), resume_text(parsed)):
        problems.append(f"{SOFT}the summary contains numbers that aren't in the resume: {sorted(made_up)}")
    original = resume_text(parsed).lower()
    invented = [s for s in data.get("skills", []) if s.lower() not in original]
    if invented:
        problems.append(f"skills not present in the original resume (don't add skills the candidate didn't list): {', '.join(invented[:8])}")
    return problems[:16]


# --- Prompts -----------------------------------------------------------------------------------------


PARSER_PROMPT = """You are the Parser Agent in a resume refinement pipeline. Your only job is to turn the raw text of a resume \
into structured data. Do not judge, improve, shorten or reword anything.

Rules:
- Copy every bullet point verbatim, one array entry each, without the bullet character. Join a bullet that the PDF broke \
across lines. Never invent a bullet, a date or a company.
- Put project work or volunteering that has bullets under "experience" too (company = the project or organisation).
- "dates" are as written ("Jan 2021 - Present"). Use "" for anything the resume doesn't state.
- Skills: one entry per individual skill ("Python", "PostgreSQL"), not a category label.
- If the text looks two-column and the order is scrambled, still reconstruct each job with its own bullets.

RESUME TEXT:
{resume_text}"""

ATS_PROMPT = """You are the ATS Compatibility Agent. Applicant tracking systems parse resumes into fields; layouts and \
wording that confuse them cost candidates interviews. Judge this resume as an ATS would, using BOTH the parsed structure \
and the measured PDF layout below. The measurements are facts from the file; the parse shows what a machine got out of it.

Check exactly these, and report only real problems:
1. Multi-column layout (text read in the wrong order, columns merged).
2. Tables used for layout or skills grids.
3. Images, logos, icons or graphics in place of text (skill bars, rating dots, icon contact details, a photo).
4. Text in the page header/footer (ATS often drops it) - contact details especially.
5. Non-standard section headers (e.g. "My Journey", "Toolbox", "Where I've Been" instead of Experience / Education / Skills / Summary).
6. Missing standard sections (Summary, Experience, Education, Skills): name the section in the title.
7. Contact details missing or unparseable; inconsistent or ambiguous date formats; decorative special characters.
8. Length: more than two pages for under ten years of experience.

Severity: "blocker" = an ATS will likely lose or scramble content (columns, tables, text in images, missing contact info). \
"warning" = a risk or best-practice gap. Explain each issue in plain language a non-technical person understands, cite the \
measurement or section that proves it in "evidence", and give a concrete fix. If the layout is clean, say so in the summary and \
return few or no issues; never pad the list. Score 100 = parses perfectly.

MEASURED PDF LAYOUT:
{layout}

SECTION HEADERS FOUND IN THE TEXT:
{headers}

PARSED STRUCTURE:
{parsed}"""

CONTENT_PROMPT = """You are the Content & Impact Agent: a hard-nosed resume coach reviewing bullet points one at a time. \
Below, every experience bullet has an id. Review EVERY bullet and return an entry for each id.

Flag a bullet with any of:
- weak_opening_verb: starts with "Responsible for", "Worked on", "Helped", "Assisted", "Involved in", "Participated" or similar, or \
with no action verb at all.
- no_metric: describes an outcome or scope but gives no number (percent, count, time, money, users, team size, volume) where one is plausible.
- vague_claim: could appear on anyone's resume ("improved performance", "various projects", "team player", "strong results") with no specifics.
- passive_voice: "was developed by", "was responsible for", "tasks were completed".
A bullet can have several flags. A strong bullet (active verb, specific, quantified) gets flags [] and rewrite "".

For every flagged bullet write a SPECIFIC rewrite, not generic advice: keep the candidate's real facts and technologies, open with a \
strong action verb, state the action then the result. Add NOTHING the original does not say: no new outcome, purpose, frequency \
("per quarter"), scale or scope ("across platforms"). The only additions allowed are a stronger verb, tighter wording, and \
[placeholders] where a number would belong. A trailing clause that explains the benefit is an addition: BAD "Led usability testing, \
ensuring cohesive design and functionality"; GOOD "Led usability testing across [N] sessions". Pick a verb that does not overstate the \
person's role: "Helped" becomes "Supported" or "Contributed to", never "Led" or "Managed"; "Served customers" stays "Served customers". NEVER invent a number, tool, employer or outcome. Every number in a rewrite must already be in the original bullet; where a metric is plausible but not given, put a \
clearly marked placeholder such as [X%] or [N users] for the candidate to fill in. Tense: bullets under a CURRENT role use the present \
tense ("Lead", "Maintain"); bullets under a PAST role use the past tense ("Led", "Developed"): see the tag after each job. \
"explanation" says what is wrong with THIS bullet in one sentence that quotes or points to its words. \
"summary_review" is a short critique of the professional summary (clichés like "passionate" or "team player", no specifics).

RESUME (bullets are labelled with their ids):
{numbered}"""

JOB_MATCH_PROMPT = """You are the Job-Match Agent. Compare the resume to the job description and report honestly how well it fits.

1. matched_keywords: skills, tools, qualifications and domain terms from the job description that the resume really shows; \
"evidence" says where (company/bullet).
2. missing_keywords: important terms from the job description the resume does not show. importance = high/medium/low by how \
central the job description makes them. "suggestion": if an existing experience could honestly carry it, say which and how; if it is \
a true gap, say so. Never suggest claiming experience the candidate doesn't have.
3. requirement_matches: take the job description's main requirements (6-12) and for each give the ids of the resume bullets that \
best support it (strength strong/partial/weak, or "none" with empty bullet_ids), plus a short note.
A requirement met by education, certifications or the skills list rather than a bullet gets empty bullet_ids and a note saying where \
it is shown. Use only bullet ids from the resume below.

JOB DESCRIPTION:
{job_description}

RESUME (bullets are labelled with their ids):
{numbered}"""

REWRITE_PROMPT = """You are the Rewrite Agent. Three specialists have already reviewed this resume. Produce the improved draft.

Hard rules:
- Keep the structure: the same jobs, same order, same company/title/dates. Every original bullet appears EXACTLY ONCE, with \
"source_id" set to its original id (e.g. e1.b2). You may reorder bullets within a job.
- Apply the Content Agent's suggested rewrite for each flagged bullet (improve its wording further if you can, but keep the facts \
and add no outcome, purpose, frequency or scope the original doesn't state). Bullets it did not flag stay exactly as they are.
- Without a job description, keep the bullets of each job in their original order.
- Keep [placeholders] such as [X%] exactly as given: the candidate must fill them in. Never invent numbers, tools, employers, \
frequencies ("bi-weekly") or outcomes ("zero defects"): every number must already be in the original bullet.
- Do not pad bullets with explanatory clauses ("..., ensuring cohesive design"): a rewritten bullet says what the original says, with a \
stronger verb and [placeholders] for missing numbers, nothing more. Do not upgrade a verb past the person's real role.
- Tense: bullets of a PAST role stay in the past tense, bullets of the CURRENT role in the present tense (see the tags).
- A bullet the Content Agent did not flag stays word for word as it is, unless a job description is given and a small edit makes its \
match to the job clearer.
- If a job description was given: within each job put the bullets that best match its requirements first (use the Job-Match \
Agent's requirement matches), weave in its missing keywords ONLY where the existing experience honestly supports them, and tailor the \
summary to the role. Do not add skills the resume doesn't already list; reorder "skills" so the matched ones come first.
- The summary: tighten it (2-3 sentences, specific, no clichés); if the resume has none and a job description is given, write one from the \
resume's real facts.
- "notes": two or three sentences on what you changed and why.

ORIGINAL RESUME (bullets labelled with their ids):
{numbered}

CONTENT AGENT FEEDBACK (per bullet):
{content}

ATS ISSUES TO KEEP IN MIND:
{ats}

{job_section}"""


# --- Running an agent ------------------------------------------------------------------------------------


@dataclass
class AgentRun:
    """One agent's outcome, shaped for storage and the UI."""

    key: str
    output: dict[str, Any] | None = None
    error: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)  # [{raw, problems}] one per model reply
    warnings: list[str] = field(default_factory=list)  # soft problems still present in the accepted reply
    provider_used: str | None = None
    model: str | None = None
    mock: bool = False
    fallback_errors: list[dict[str, Any]] = field(default_factory=list)
    prompt: str = ""
    duration_ms: int = 0


async def run_agent(
    context: NodeContext, key: str, prompt: str, schema: dict[str, Any], *, provider: str, fallback: list[str],
    check: "Any" = None, system: str = "", temperature: float = 0.2, max_tokens: int = 8192,
) -> AgentRun:
    """Ask `provider` (with `fallback`) for JSON matching `schema` that also passes `check(data) -> problems`;
    one retry with the problems listed. The raw reply of every attempt is kept."""
    run = AgentRun(key=key, prompt=prompt)
    started = time.monotonic()
    request = schema_request(prompt, schema)
    system_prompt = (system + " " if system else "") + "You reply with JSON only."
    problems: list[str] = []
    best: tuple[dict[str, Any], list[str]] | None = None  # the latest reply that had only soft problems
    for attempt in range(1, MAX_ATTEMPTS + 1):
        user_prompt = request if attempt == 1 else retry_request(request, problems)
        try:
            answer = await generate_with_fallback(
                context, provider=provider, model=None, fallback=fallback, system_prompt=system_prompt,
                user_prompt=user_prompt, temperature=temperature, max_tokens=max_tokens,
            )
        except LLMChainFailed as exc:
            run.error = str(exc)
            run.fallback_errors += exc.errors
            break
        run.fallback_errors += answer.fallback_errors
        run.provider_used, run.model, run.mock = answer.provider_used, answer.model, answer.mock
        data, problems = check_reply(answer.text, schema)
        if not problems and check is not None:
            problems = check(data)
        run.attempts.append({"raw": answer.text, "problems": problems})
        if not problems:
            run.output = data
            break
        if data is not None and all(p.startswith(SOFT) for p in problems):
            best = (data, [p.lstrip(SOFT) for p in problems])
        if attempt == MAX_ATTEMPTS:
            if best is not None:  # accepted with warnings; the assembler corrects what it can
                run.output, run.warnings = best
                break
            run.error = f"The model's reply failed validation after {MAX_ATTEMPTS} attempts: {'; '.join(p.lstrip(SOFT) for p in problems[:5])}"
    if run.output is None and run.error is None and best is not None:
        run.output, run.warnings = best
    run.duration_ms = round((time.monotonic() - started) * 1000)
    return run


def sanitize_content(data: dict[str, Any], bullets: dict[str, Any]) -> dict[str, Any]:
    """The Content Agent's output without entries for unknown bullet ids or repeated reviews."""
    seen: set[str] = set()
    kept = []
    for item in data["bullets"]:
        if item["id"] in bullets and item["id"] not in seen:
            seen.add(item["id"])
            kept.append(item)
    return {**data, "bullets": kept}


def sanitize_job_match(data: dict[str, Any], bullets: dict[str, Any]) -> dict[str, Any]:
    """The Job-Match Agent's output with unknown bullet ids removed from its requirement matches."""
    return {**data, "requirement_matches": [
        {**m, "bullet_ids": [b for b in m["bullet_ids"] if b in bullets]} for m in data["requirement_matches"]
    ]}


def fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[... cut]"


def render_prompt(template: str, **values: Any) -> str:
    return template.format(**{k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)) for k, v in values.items()})


def schema_problems(data: Any, schema: dict[str, Any]) -> list[str]:
    return validate(data, schema)


def deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)
