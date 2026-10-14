"""PDF preparation with pypdfium2.

pypdfium2 wraps Google's PDFium (the PDF engine in Chrome). It is licensed
Apache-2.0/BSD, which is fine for closed-source SaaS. (PyMuPDF is AGPL and must
NOT be used, see requirements §6.)

For each page we produce:
  * `text`  - the PDF's own text layer (exact characters), or None for scans;
  * `image` - a rendered, downscaled JPEG of the page.

Digital pages get BOTH: the text keeps digits exact, the image keeps the table
layout. Scanned pages get only the image (there is no real text to extract).

Security: PDFs can contain JavaScript, forms and embedded files. We never run
any of it: we only read the text layer and render pixels.
"""

from __future__ import annotations

import pypdfium2 as pdfium

from app.config import settings
from app.core.errors import JobError

from .image import DIGITAL_MAX_EDGE, SCAN_MAX_EDGE, downscale, to_jpeg

# A page with fewer readable characters than this is treated as a scan.
_MIN_TEXT_CHARS = 50
# Never render a page larger than this many pixels on its long edge, whatever
# size the PDF claims (protects memory against malicious page sizes).
_MAX_RENDER_EDGE = 4000


def open_pdf(data: bytes) -> pdfium.PdfDocument:
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        # PDFium reports password-protected files as a "password" error.
        if "password" in str(e).lower():
            raise JobError("encrypted_document") from None
        raise JobError("corrupt_file") from None
    if len(pdf) == 0:
        raise JobError("corrupt_file")
    if len(pdf) > settings.max_pages:
        raise JobError("too_many_pages")          # FR-2.5
    return pdf


def pdf_pages(data: bytes) -> list[dict]:
    """Return one dict per page: {"text": str|None, "image": bytes, "scanned": bool}."""
    pdf = open_pdf(data)
    pages: list[dict] = []
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            text = page.get_textpage().get_text_range() or ""
            scanned = len(text.strip()) < _MIN_TEXT_CHARS

            # Render at 150 DPI for scans (need detail), 100 DPI for digital pages.
            # PDF units are 1/72 inch, so scale = DPI / 72.
            width, height = page.get_size()
            scale = (150 if scanned else 100) / 72
            longest = max(width, height) * scale
            if longest > _MAX_RENDER_EDGE:
                scale *= _MAX_RENDER_EDGE / longest
            bitmap = page.render(scale=scale)
            img = downscale(bitmap.to_pil(), SCAN_MAX_EDGE if scanned else DIGITAL_MAX_EDGE)
            pages.append({"text": None if scanned else text, "image": to_jpeg(img), "scanned": scanned})
    finally:
        pdf.close()
    return pages
