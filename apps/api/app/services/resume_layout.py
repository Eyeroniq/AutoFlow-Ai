"""Measuring a resume PDF's layout, for the ATS Compatibility Agent.

What an applicant tracking system trips over is mostly visible in the file, not in the words:
two columns, tables, text that is really an image, contact details in the page header, odd section
names. These are measured here with PyMuPDF (facts, not opinions) and handed to the agent as
evidence; the clear-cut findings are also turned into issues directly (`layout_issues`), so a
blocker the model overlooks still reaches the report.
"""

import re
from typing import Any

import pymupdf

# Header/footer zones: the top and bottom share of the page height.
EDGE_ZONE = 0.07
ICON_MAX_POINTS = 64  # an image up to this wide and tall is an icon or logo (1 pt = 1/72 inch)
IMAGE_BIG_SHARE = 0.06  # an image covering this much of a page may be carrying text

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?:\+?\d[\d\s().-]{8,}\d)")
STANDARD_HEADERS = {
    "summary", "professional summary", "profile", "objective", "career objective", "experience", "work experience",
    "professional experience", "employment", "employment history", "work history", "education", "skills",
    "technical skills", "key skills", "projects", "certifications", "certificates", "awards", "publications",
    "languages", "achievements", "volunteering", "volunteer experience", "interests",
}


def _page_lines(page: Any) -> list[tuple[float, float, float, float, int]]:
    """(x0, y0, x1, y1, word count) of every text line on the page."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                x0, y0, x1, y1 = line["bbox"]
                lines.append((x0, y0, x1, y1, len(text.split())))
    return lines


# Lines needed on each side of a gutter, and rows where both sides have text, to call it two columns.
MIN_SIDE_LINES = 6
MIN_SHARED_ROWS = 4


def _columns(lines: list[tuple[float, float, float, float, int]], width: float) -> dict[str, Any]:
    """Whether the page's text lines form two side-by-side columns: a vertical gutter that (almost)
    no line crosses, with many lines to its left and many to its right on the same rows. A single
    column with right-aligned dates has few lines right of any gutter, so it doesn't qualify."""
    best = {"columns": 1, "left_blocks": 0, "right_blocks": 0, "side_by_side_pairs": 0}
    if len(lines) < 2 * MIN_SIDE_LINES:
        return best
    for gutter in range(int(width * 0.22), int(width * 0.72), 6):
        left = [ln for ln in lines if ln[2] <= gutter]
        right = [ln for ln in lines if ln[0] >= gutter]
        crossing = [ln for ln in lines if ln[0] < gutter < ln[2]]
        if len(left) < MIN_SIDE_LINES or len(right) < MIN_SIDE_LINES or len(crossing) > 0.12 * len(lines):
            continue
        rows = sum(1 for lb in left if any(min(lb[3], rb[3]) - max(lb[1], rb[1]) > 4 for rb in right))
        if rows >= MIN_SHARED_ROWS and rows > best["side_by_side_pairs"]:
            best = {"columns": 2, "left_blocks": len(left), "right_blocks": len(right), "side_by_side_pairs": rows}
    return best


def _header_candidates(page: Any) -> list[str]:
    """Lines that look like section headers: short, and bold or larger than the body text."""
    sizes: list[float] = []
    lines: list[tuple[str, float, bool]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = " ".join(s["text"].strip() for s in spans).strip()
            size = max(s["size"] for s in spans)
            bold = all(bool(s["flags"] & 16) or "bold" in s["font"].lower() for s in spans)
            sizes += [s["size"] for s in spans for _ in range(len(s["text"]))]
            lines.append((text, size, bold))
    if not sizes:
        return []
    body = sorted(sizes)[len(sizes) // 2]
    return [
        text for text, size, bold in lines
        if 1 <= len(text.split()) <= 4 and len(text) <= 40 and sum(c.isalpha() for c in text) >= 3
        and (bold or size > body * 1.15 or text.isupper()) and not EMAIL.search(text)
    ]


def analyze_pdf(path: str) -> dict[str, Any]:
    """Layout measurements of the PDF at `path` (JSON-serializable)."""
    pages: list[dict[str, Any]] = []
    headers: list[str] = []
    fonts: set[str] = set()
    edge_text: list[str] = []
    with pymupdf.open(path) as document:
        for number, page in enumerate(document, start=1):
            width, height = page.rect.width, page.rect.height
            blocks = [b[:5] for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
            layout = _columns(_page_lines(page), width)
            images = []
            for info in page.get_image_info():
                box = pymupdf.Rect(info["bbox"])
                images.append({"width": round(box.width), "height": round(box.height), "share": round(box.get_area() / (width * height), 3)})
            icons = [i for i in images if i["width"] <= ICON_MAX_POINTS and i["height"] <= ICON_MAX_POINTS]
            big = [i for i in images if i["share"] >= IMAGE_BIG_SHARE]
            tables = []
            try:
                for table in page.find_tables().tables:
                    rows, cols = table.row_count, table.col_count
                    if rows >= 2 and cols >= 2:
                        tables.append({"rows": rows, "columns": cols})
            except Exception:  # find_tables can fail on odd drawings; the rest of the analysis stands
                pass
            top, bottom = height * EDGE_ZONE, height * (1 - EDGE_ZONE)
            header_text = " ".join(b[4] for b in blocks if b[3] <= top).strip()
            footer_text = " ".join(b[4] for b in blocks if b[1] >= bottom).strip()
            edge_text += [header_text, footer_text]
            for span_font in {f[3] for f in page.get_fonts()}:
                fonts.add(span_font)
            chars = sum(len(b[4]) for b in blocks)
            headers += _header_candidates(page)
            pages.append({
                "page": number, "text_chars": chars, "text_blocks": len(blocks), **layout, "images": len(images),
                "icon_images": len(icons), "large_images": len(big), "tables": tables,
                "header_zone_text": header_text[:160], "footer_zone_text": footer_text[:160],
                "links": len(page.get_links()),
            })
        page_count = document.page_count
    edge_blob = " ".join(edge_text)
    seen: set[str] = set()
    unique_headers = [h for h in headers if not (h.lower() in seen or seen.add(h.lower()))]
    return {
        "page_count": page_count,
        "pages": pages,
        "fonts": sorted(fonts)[:12],
        "font_count": len(fonts),
        "contact_in_header_footer": bool(EMAIL.search(edge_blob) or PHONE.search(edge_blob)),
        "section_headers": unique_headers[:30],
        "nonstandard_headers": [h for h in unique_headers if h.lower().strip(":") not in STANDARD_HEADERS][:20],
        "total_text_chars": sum(p["text_chars"] for p in pages),
    }


def describe(layout: dict[str, Any]) -> str:
    """The measurements as plain lines for the agent's prompt."""
    lines = [f"- {layout['page_count']} page(s), {layout['total_text_chars']} characters of selectable text, "
             f"{layout['font_count']} font(s): {', '.join(layout['fonts'][:6])}"]
    for p in layout["pages"]:
        parts = [f"{p['columns']} text column(s)" + (f" ({p['side_by_side_pairs']} side-by-side block pairs)" if p["columns"] > 1 else "")]
        parts.append(f"{p['images']} image(s) ({p['icon_images']} icon-sized, {p['large_images']} large)")
        parts.append(f"{len(p['tables'])} table(s)" + (": " + ", ".join(f"{t['rows']}x{t['columns']}" for t in p["tables"]) if p["tables"] else ""))
        lines.append(f"- page {p['page']}: " + "; ".join(parts) + f"; {p['text_chars']} chars; {p['links']} link(s)")
        if p["header_zone_text"]:
            lines.append(f"  text in the page header zone: {p['header_zone_text']!r}")
        if p["footer_zone_text"]:
            lines.append(f"  text in the page footer zone: {p['footer_zone_text']!r}")
    lines.append(f"- contact details (email/phone) inside the header or footer zone: {'YES' if layout['contact_in_header_footer'] else 'no'}")
    return "\n".join(lines)


def layout_issues(layout: dict[str, Any]) -> list[dict[str, Any]]:
    """Findings that need no judgement: measured facts, as ATS issues."""
    issues: list[dict[str, Any]] = []
    columns = [p for p in layout["pages"] if p["columns"] > 1]
    if columns:
        issues.append({
            "category": "multi_column", "severity": "blocker", "title": "Two-column layout",
            "explanation": "The text is laid out in side-by-side columns. Many applicant tracking systems read a page line by "
                           "line across the whole width, so a job's title can end up glued to an unrelated sidebar item and "
                           "your experience comes out scrambled.",
            "evidence": f"Page(s) {', '.join(str(p['page']) for p in columns)}: {columns[0]['left_blocks']} blocks on the left and "
                        f"{columns[0]['right_blocks']} on the right overlap vertically.",
            "fix": "Use a single column, top to bottom: contact, summary, experience, education, skills.",
        })
    tables = [(p["page"], t) for p in layout["pages"] for t in p["tables"]]
    if tables:
        issues.append({
            "category": "table", "severity": "blocker", "title": "Content inside a table",
            "explanation": "Parts of the resume sit in a table. An ATS often flattens tables cell by cell, so dates, titles and "
                           "skills lose the relationship they have on the page.",
            "evidence": ", ".join(f"page {n}: {t['rows']}x{t['columns']}" for n, t in tables[:4]),
            "fix": "Replace the table with plain lines or a comma-separated skills list.",
        })
    big = sum(p["large_images"] for p in layout["pages"])
    icons = sum(p["icon_images"] for p in layout["pages"])
    low_text = layout["total_text_chars"] < 25 * layout["page_count"]
    if big or icons or low_text:
        severity = "blocker" if (big and layout["total_text_chars"] < 600) or low_text else "warning"
        what = []
        if low_text:
            what.append("almost no selectable text (the resume may be a picture of text)")
        if big:
            what.append(f"{big} large image(s)")
        if icons:
            what.append(f"{icons} icon-sized image(s)")
        issues.append({
            "category": "image_or_icon", "severity": severity, "title": "Images or icons in the resume",
            "explanation": "Text inside an image can't be read by an ATS, and icons used in place of words (a phone symbol "
                           "instead of 'Phone') leave contact details unlabelled.",
            "evidence": "; ".join(what), "fix": "Use real text for every word, label contact details in words, and drop photos and skill-bar graphics.",
        })
    if layout["contact_in_header_footer"]:
        issues.append({
            "category": "header_footer", "severity": "blocker", "title": "Contact details in the page header or footer",
            "explanation": "Many applicant tracking systems ignore page headers and footers, so a resume with its email or phone "
                           "only there can be filed with no way to contact you.",
            "evidence": "An email address or phone number was found in the top or bottom margin zone.",
            "fix": "Move your contact details into the body, at the top of page 1.",
        })
    return issues
