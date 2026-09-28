"""Builds samples/scanned-invoice.pdf: a two-page, image-only "scan" for the document nodes.

The pages are typeset with PyMuPDF, rasterized, and degraded the way a flatbed scan is
(slight skew, uneven exposure, sensor noise, softness, JPEG compression), then written as
a PDF of images with no text layer, so PDF Extract finds nothing and the text can only
come from OCR. Everything in it (companies, people, numbers) is fictional.

Regenerate with the backend image, which has PyMuPDF and Pillow:
    docker compose run --rm --no-deps -v "$PWD/samples:/samples" api python /samples/make_scanned_sample.py
"""

import io
import random
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter

OUT = Path(__file__).with_name("scanned-invoice.pdf")
DPI = 200
A4 = pymupdf.paper_rect("a4")

PAGE_1 = """HARBOURLINE FREIGHT LTD.
14 Quay Street, Bristol BS1 4DB, United Kingdom
accounts@harbourline.example   VAT GB 204 5561 07

INVOICE
Invoice number: HF-2026-0417
Invoice date: 12 March 2026
Payment due: 11 April 2026

Bill to:
Bluefield Retail GmbH
Attn: Maria Schneider, Head of Procurement
Speicherstadt 8, 20457 Hamburg, Germany
Purchase order: PO-88213

Description                                              Amount (EUR)
Sea freight, Bristol to Hamburg, 2 x 40ft containers         3,200.00
Customs clearance and handling                                  450.00
Cargo insurance (1.2% of declared value)                         38.40
Subtotal                                                       3,688.40
VAT 19%                                                          700.80
TOTAL DUE                                                 EUR 4,389.20

Please pay by bank transfer, quoting HF-2026-0417, by 11 April 2026.
Late payments incur interest of 1.5% per month."""

PAGE_2 = """HARBOURLINE FREIGHT LTD.

Bristol, 12 March 2026

Dear Ms Schneider,

Thank you for choosing Harbourline Freight for the March shipment. The two
containers left Bristol on 5 March 2026 aboard the MV Severn Star and were
delivered to your Hamburg warehouse on 10 March 2026, one day ahead of schedule.

As agreed with Thomas Becker of your logistics team, the enclosed invoice
covers freight, customs clearance, and insurance only. Storage at the port was
waived as a goodwill gesture (a saving of EUR 275.00).

Our next scheduled sailing to Hamburg is on 2 April 2026. If you would like to
book space, please contact our operations desk before 25 March 2026.

Kind regards,

James O'Connor
Accounts Director, Harbourline Freight Ltd."""


def typeset(text: str) -> bytes:
    """One page of text as a grayscale PNG at DPI."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    lines = text.splitlines()
    y = 72.0
    for i, line in enumerate(lines):
        heading = i == 0 or line in {"INVOICE"}
        size = 15 if heading else 10.5
        font = "cour" if "  " in line.strip() else ("hebo" if heading else "helv")
        page.insert_text((64, y), line, fontsize=size, fontname=font)
        y += size * 1.55
    pixmap = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    doc.close()
    return pixmap.tobytes("png")


def degrade(png: bytes, rng: random.Random) -> bytes:
    """Scanner-like damage; returns JPEG bytes."""
    image = Image.open(io.BytesIO(png)).convert("L")
    image = image.rotate(rng.uniform(-0.9, 0.9), resample=Image.BICUBIC, expand=False, fillcolor=250)
    width, height = image.size
    # Uneven exposure: a slightly darker band, like a lid that didn't close flat.
    gradient = Image.linear_gradient("L").resize((width, height)).point(lambda v: 235 + v * 20 // 255)
    image = Image.blend(image, Image.eval(gradient, lambda v: v), 0.08)
    # Sensor noise and softness.
    noise = Image.effect_noise((width, height), 18)
    image = Image.blend(image, noise, 0.07)
    image = image.filter(ImageFilter.GaussianBlur(0.6))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=62)
    return out.getvalue()


def main() -> None:
    rng = random.Random(417)  # deterministic output
    doc = pymupdf.open()
    for text in (PAGE_1, PAGE_2):
        scan = degrade(typeset(text), rng)
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_image(page.rect, stream=scan)
    doc.set_metadata({"title": "Scanned invoice (sample)", "producer": "samples/make_scanned_sample.py"})
    doc.save(OUT, garbage=4, deflate=True)
    doc.close()
    check = pymupdf.open(OUT)
    text_chars = sum(len(page.get_text().strip()) for page in check)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KiB, {check.page_count} pages, text-layer chars: {text_chars})")


if __name__ == "__main__":
    main()
