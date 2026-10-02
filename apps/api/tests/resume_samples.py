"""Sample resume PDFs for tests and demos (reportlab), each with known flaws:

- priya: a clean single-column backend-engineer resume, bullets of mixed quality (some weak verbs,
  some vague, some without numbers, some strong).
- marcus: ATS-hostile: two columns with a sidebar, a skills table, icon images, non-standard section
  names, and his email and phone only in the page header.
- aisha: a thin graduate resume with no summary and no skills section.

    python -m tests.resume_samples ../../samples/resumes     (from apps/api)
"""

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PRIYA_JD = """Senior Backend Engineer (Python) - Meridian Payments

We are looking for a Senior Backend Engineer to build and scale the services behind our payments platform.

Responsibilities
- Design, build and operate Python (FastAPI or Django) services that handle high request volumes
- Own PostgreSQL schemas and query performance; experience with Redis caching
- Run services on AWS using Docker and Kubernetes; build CI/CD pipelines
- Improve reliability: monitoring, alerting, incident response, on-call
- Mentor engineers and review code

Requirements
- 5+ years of backend development, strong Python
- Experience with event-driven systems (Kafka or similar message queues)
- Hands-on with AWS (ECS/EKS, RDS, S3), Terraform is a plus
- Comfortable with REST API design, testing, and observability (Prometheus, Grafana)
- Track record of measurable performance or reliability improvements
"""

AISHA_JD = """Junior Data Analyst - BrightRetail

Join our analytics team to turn retail data into decisions.
- Write SQL queries and build dashboards in Tableau or Power BI
- Clean and analyse data with Python (pandas) or Excel
- Present insights to non-technical stakeholders
- Run A/B test analysis and report results
Requirements: degree in a quantitative field, SQL, basic statistics, strong communication, internship or project experience.
"""


MARCUS_JD = """Senior Product Designer - Northwind Health

Northwind builds patient-facing mobile and web products used by millions of people to manage their care.

What you'll do
- Own end-to-end design for core patient flows (booking, messaging, prescriptions), from research to shipped UI
- Run user research and usability testing and turn findings into design decisions
- Build and maintain our design system in Figma and partner with engineers on implementation
- Define success metrics with product managers and show impact through experiments
- Mentor designers and raise the quality bar

What we're looking for
- 6+ years in product or UX design with a strong mobile portfolio
- Expert Figma and prototyping skills; comfortable with HTML/CSS
- Experience with accessibility (WCAG) and design systems at scale
- A track record of measurable improvements to conversion or task completion
"""

def _single_column(story_builder) -> bytes:
    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontName="Helvetica", fontSize=10, leading=13)
    head = ParagraphStyle("h", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=12, spaceBefore=10, spaceAfter=3)
    name = ParagraphStyle("n", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, alignment=1)
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=0.8 * inch, rightMargin=0.8 * inch, topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    doc.build(story_builder(body, head, name))
    return out.getvalue()


def _bullets(body: ParagraphStyle, items: list[str]) -> ListFlowable:
    return ListFlowable([ListItem(Paragraph(t, body), leftIndent=14) for t in items], bulletType="bullet", start="•", leftIndent=14)


def priya() -> bytes:
    def story(body, head, name):
        s = [Paragraph("Priya Nair", name),
             Paragraph("priya.nair@example.com | +91 98765 43210 | Pune, India | linkedin.com/in/priyanair | github.com/pnair", ParagraphStyle("c", parent=body, alignment=1))]
        s += [Paragraph("SUMMARY", head), Paragraph(
            "Backend engineer with 6 years of experience who is passionate about building software. Hard-working team player "
            "looking for new challenges in a dynamic environment.", body)]
        s += [Paragraph("EXPERIENCE", head)]
        s += [Paragraph("<b>Senior Software Engineer</b>, FinServe Technologies (Mar 2021 - Present)", body)]
        s += [_bullets(body, [
            "Responsible for the backend of the merchant onboarding platform, built with Python and FastAPI.",
            "Led the migration of a monolithic Django application to 7 microservices on AWS ECS, cutting deploy time from 3 hours to 20 minutes.",
            "Worked on improving database performance for the reporting service.",
            "Was involved in the design of an event-driven notification pipeline using Kafka.",
            "Mentored 4 junior engineers and ran weekly code reviews, reducing review turnaround from 2 days to 6 hours.",
            "Helped with various production incidents and on-call duties.",
        ])]
        s += [Spacer(1, 6), Paragraph("<b>Software Engineer</b>, ShopKart (Jul 2018 - Feb 2021)", body)]
        s += [_bullets(body, [
            "Developed REST APIs for the checkout service used by many customers.",
            "Introduced Redis caching on product search which lowered p95 latency from 900 ms to 210 ms and saved about $40k per year in infrastructure costs.",
            "Writing unit and integration tests was done to improve code quality.",
            "Worked with the QA team on releases.",
        ])]
        s += [Paragraph("EDUCATION", head), Paragraph("B.E. Computer Engineering, Savitribai Phule Pune University (2014 - 2018), CGPA 8.7/10", body)]
        s += [Paragraph("SKILLS", head), Paragraph("Python, FastAPI, Django, PostgreSQL, Redis, Kafka, AWS (ECS, RDS, S3), Docker, Git, REST APIs, Linux", body)]
        return s

    return _single_column(story)


def _icon(kind: str) -> ImageReader:
    image = Image.new("RGB", (48, 48), (30, 60, 110))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 40, 40), outline=(255, 255, 255), width=4)
    draw.rectangle((20, 20, 28, 28), fill=(255, 255, 255)) if kind == "mail" else draw.line((12, 36, 36, 12), fill=(255, 255, 255), width=4)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return ImageReader(buffer)


def marcus() -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    width, height = A4
    # Contact details only in the page header.
    c.setFont("Helvetica", 8)
    c.drawString(40, height - 22, "marcus.lee@example.com   |   +1 415 555 0147   |   Austin, TX")
    # Sidebar with a background colour, icons instead of labels.
    c.setFillColor(colors.HexColor("#1E3C6E"))
    c.rect(0, 0, 190, height, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(24, height - 80, "Marcus Lee")
    c.setFont("Helvetica", 10)
    c.drawString(24, height - 98, "UX / Product Designer")
    c.drawImage(_icon("mail"), 24, height - 150, 22, 22)
    c.drawImage(_icon("phone"), 24, height - 182, 22, 22)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(24, height - 240, "TOOLBOX")
    c.setFont("Helvetica", 10)
    y = height - 260
    for item in ["Figma", "Sketch", "Adobe XD", "Prototyping", "User research", "HTML / CSS", "Design systems"]:
        c.drawString(24, y, item)
        y -= 16
    c.setFont("Helvetica-Bold", 11)
    c.drawString(24, y - 18, "LANGUAGES")
    c.setFont("Helvetica", 10)
    c.drawString(24, y - 36, "English, Spanish")

    c.setFillColor(colors.black)
    x = 215
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, height - 80, "ABOUT ME")
    c.setFont("Helvetica", 10)
    for i, line in enumerate(["Creative designer who loves solving problems and making things beautiful.",
                              "Always learning, always curious. Coffee enthusiast."]):
        c.drawString(x, height - 98 - i * 14, line)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, height - 150, "MY JOURNEY")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, height - 172, "Lead Product Designer - Lumen Health  (2020 - Now)")
    c.setFont("Helvetica", 10)
    lines = [
        "• Responsible for the design of the patient mobile app",
        "• Redesigned the appointment booking flow, which improved completion by 23%",
        "• Worked with developers and product managers on many features",
        "• Created a design system used by 3 product teams",
        "• Ran user interviews",
    ]
    for i, line in enumerate(lines):
        c.drawString(x, height - 190 - i * 15, line)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, height - 290, "UX Designer - Brightpath Agency  (2016 - 2020)")
    c.setFont("Helvetica", 10)
    lines = [
        "• Designed websites and apps for various clients",
        "• Was in charge of the agency's usability testing process",
        "• Delivered 40+ projects for startups and retailers",
        "• Collaborated across teams to make great products",
    ]
    for i, line in enumerate(lines):
        c.drawString(x, height - 308 - i * 15, line)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, height - 400, "WHERE I STUDIED")
    c.setFont("Helvetica", 10)
    c.drawString(x, height - 420, "BFA Graphic Design, University of Texas at Austin (2012 - 2016)")
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, height - 462, "SKILL LEVELS")
    # A table (ruled grid) of skill ratings.
    c.setFont("Helvetica", 10)
    table = Table([["Figma", "Expert"], ["Prototyping", "Advanced"], ["HTML / CSS", "Intermediate"]], colWidths=[130, 110])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 10)]))
    table.wrapOn(c, 240, 80)
    table.drawOn(c, x, height - 540)
    c.showPage()
    c.save()
    return out.getvalue()


def aisha() -> bytes:
    def story(body, head, name):
        s = [Paragraph("Aisha Khan", name),
             Paragraph("aisha.khan@example.com | +44 7700 900123 | Manchester, UK", ParagraphStyle("c", parent=body, alignment=1))]
        s += [Paragraph("EDUCATION", head), Paragraph(
            "BSc Mathematics and Statistics, University of Manchester (2020 - 2023), 2:1. "
            "Modules: Regression, Probability, Databases, R programming.", body)]
        s += [Paragraph("WORK EXPERIENCE", head), Paragraph("<b>Data Intern</b>, GreenGrocer Ltd (Jun 2022 - Sep 2022)", body)]
        s += [_bullets(body, [
            "Helped the analytics team with weekly reports.",
            "Used Excel and some SQL to look at sales data.",
            "Made charts for the manager.",
        ])]
        s += [Spacer(1, 6), Paragraph("<b>Customer Assistant</b>, Costa Coffee (2019 - 2022)", body)]
        s += [_bullets(body, [
            "Served customers and handled cash.",
            "Trained 3 new team members during busy periods.",
        ])]
        s += [Paragraph("PROJECTS", head), Paragraph("Dissertation: analysed 12,000 rows of NHS appointment data in R to study missed-appointment rates.", body)]
        return s

    return _single_column(story)


SAMPLES = {"priya-nair": (priya, PRIYA_JD), "marcus-lee": (marcus, MARCUS_JD), "aisha-khan": (aisha, AISHA_JD)}


def write_all(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for stem, (build, jd) in SAMPLES.items():
        path = folder / f"{stem}.pdf"
        path.write_bytes(build())
        if jd:
            (folder / f"{stem}-job-description.txt").write_text(jd, encoding="utf-8")
        paths.append(path)
    return paths


if __name__ == "__main__":
    for written in write_all(Path(sys.argv[1] if len(sys.argv) > 1 else "resumes")):
        print(written)
