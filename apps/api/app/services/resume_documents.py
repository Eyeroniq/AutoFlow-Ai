"""The refined resume as a PDF and a DOCX file.

Both are deliberately plain: one column, standard section names, real text, real bullet
paragraphs, no tables, images or icons, so the file passes the checks the ATS Agent applies.
`resume` is the structure the Parser Agent produces (bullets as plain strings).
"""

import io
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape

import reportlab

import os

_FONT_DIR = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
_REGISTERED = False


def _fonts() -> tuple[str, str]:
    """Bitstream Vera ships with reportlab and covers Latin-1 and Latin Extended (the built-in
    Helvetica is Latin-1 only), regular and bold."""
    global _REGISTERED
    if not _REGISTERED:
        pdfmetrics.registerFont(TTFont("Vera", os.path.join(_FONT_DIR, "Vera.ttf")))
        pdfmetrics.registerFont(TTFont("Vera-Bold", os.path.join(_FONT_DIR, "VeraBd.ttf")))
        pdfmetrics.registerFontFamily("Vera", normal="Vera", bold="Vera-Bold")
        _REGISTERED = True
    return "Vera", "Vera-Bold"


def contact_line(contact: dict[str, Any]) -> str:
    parts = [contact.get("email", ""), contact.get("phone", ""), contact.get("location", ""), *contact.get("links", [])]
    return "  |  ".join(p.strip() for p in parts if p and p.strip())


def _dated(job: dict[str, Any]) -> str:
    head = ", ".join(p for p in (job.get("title", ""), job.get("company", "")) if p)
    return head


def build_docx(resume: dict[str, Any]) -> bytes:
    document = Document()
    for section in document.sections:
        section.left_margin = section.right_margin = Inches(0.8)
        section.top_margin = section.bottom_margin = Inches(0.7)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(2)

    def heading(text: str) -> None:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(10)
        paragraph.paragraph_format.space_after = Pt(3)
        run = paragraph.add_run(text.upper())
        run.bold = True
        run.font.size = Pt(11.5)
        run.font.color.rgb = RGBColor(0x1F, 0x2A, 0x44)

    contact = resume.get("contact_info", {})
    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    name = title.add_run(contact.get("name", "").strip() or "Resume")
    name.bold = True
    name.font.size = Pt(18)
    if line := contact_line(contact):
        sub = document.add_paragraph(line)
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if resume.get("summary", "").strip():
        heading("Summary")
        document.add_paragraph(resume["summary"].strip())
    if resume.get("experience"):
        heading("Experience")
        for job in resume["experience"]:
            head = document.add_paragraph()
            head.paragraph_format.space_before = Pt(5)
            head.paragraph_format.keep_with_next = True
            run = head.add_run(_dated(job))
            run.bold = True
            if job.get("dates"):
                head.add_run(f"  ({job['dates']})")
            for bullet in job.get("bullets", []):
                document.add_paragraph(bullet, style="List Bullet")
    if resume.get("education"):
        heading("Education")
        for school in resume["education"]:
            line = ", ".join(p for p in (school.get("degree", ""), school.get("institution", "")) if p)
            if school.get("dates"):
                line += f"  ({school['dates']})"
            document.add_paragraph(line)
            if school.get("details", "").strip():
                document.add_paragraph(school["details"].strip())
    if resume.get("skills"):
        heading("Skills")
        document.add_paragraph(", ".join(resume["skills"]))
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def build_pdf(resume: dict[str, Any]) -> bytes:
    regular, bold = _fonts()
    body = ParagraphStyle("body", fontName=regular, fontSize=9.6, leading=12.6, spaceAfter=2)
    head = ParagraphStyle("head", parent=body, fontName=bold, fontSize=10.8, leading=14, spaceBefore=9, spaceAfter=2,
                          textColor="#1F2A44")
    job_style = ParagraphStyle("job", parent=body, fontName=bold, spaceBefore=5, spaceAfter=1)
    name_style = ParagraphStyle("name", parent=body, fontName=bold, fontSize=17, leading=21, alignment=TA_CENTER)
    contact_style = ParagraphStyle("contact", parent=body, alignment=TA_CENTER, spaceAfter=4)

    def p(text: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(escape(text), style)

    contact = resume.get("contact_info", {})
    story: list[Any] = [p(contact.get("name", "").strip() or "Resume", name_style)]
    if line := contact_line(contact):
        story.append(p(line, contact_style))

    def section(title: str) -> None:
        story.append(p(title.upper(), head))
        story.append(HRFlowable(width="100%", thickness=0.6, color="#1F2A44", spaceAfter=3))

    if resume.get("summary", "").strip():
        section("Summary")
        story.append(p(resume["summary"].strip(), body))
    if resume.get("experience"):
        section("Experience")
        for job in resume["experience"]:
            title = _dated(job) + (f"  ({job['dates']})" if job.get("dates") else "")
            story.append(p(title, job_style))
            bullets = [ListItem(p(text, body), leftIndent=12, bulletColor="#1F2A44") for text in job.get("bullets", [])]
            if bullets:
                story.append(ListFlowable(bullets, bulletType="bullet", start="•", leftIndent=12, bulletFontName=regular))
    if resume.get("education"):
        section("Education")
        for school in resume["education"]:
            line = ", ".join(x for x in (school.get("degree", ""), school.get("institution", "")) if x)
            if school.get("dates"):
                line += f"  ({school['dates']})"
            story.append(p(line, body))
            if school.get("details", "").strip():
                story.append(p(school["details"].strip(), body))
    if resume.get("skills"):
        section("Skills")
        story.append(p(", ".join(resume["skills"]), body))
    story.append(Spacer(1, 0.1 * inch))
    out = io.BytesIO()
    SimpleDocTemplate(
        out, pagesize=A4, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.65 * inch, bottomMargin=0.65 * inch,
        title=f"{contact.get('name', 'Resume')} - Resume", author=contact.get("name", ""),
    ).build(story)
    return out.getvalue()
