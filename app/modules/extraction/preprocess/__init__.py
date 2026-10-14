"""Turn an uploaded file into a list of PAGES that the parsers and models can read.

Every page is a dict:
    {"text": str | None,      # exact text layer, None for scans/photos
     "image": bytes | None,   # downscaled JPEG of the page, None for DOCX text
     "scanned": bool}         # True when the page has no usable text layer

Stage 6-7 of the walkthrough (architecture §4.5).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.core.errors import ApiError, JobError

from .detect import detect_type
from .docx import docx_pages
from .image import prepare_image
from .pdf import pdf_pages


@dataclass
class Prepared:
    kind: str            # "pdf" | "docx" | "jpeg" | "png"
    pages: list[dict]

    @property
    def page_count(self) -> int:
        """Number of pages prepared (PDF pages, ~3,000-character DOCX chunks, or 1 for an image)."""
        return len(self.pages)

    @property
    def is_digital_pdf(self) -> bool:
        """True if EVERY page has a real text layer: the precondition for Rung 0 parsers."""
        return self.kind == "pdf" and all(not p["scanned"] for p in self.pages)


def preprocess(data: bytes) -> Prepared:
    """Detect the type, validate, and build pages. Raises JobError for bad files."""
    try:
        kind = detect_type(data)
    except ApiError as e:
        # In the worker there is no HTTP request to answer: a bad file fails the JOB.
        raise JobError(e.code) from None
    if kind == "pdf":
        pages = pdf_pages(data)
    elif kind == "docx":
        pages = docx_pages(data, settings.max_pages)
    else:  # jpeg / png: a single page
        pages = [{"text": None, "image": prepare_image(data), "scanned": True}]
    return Prepared(kind=kind, pages=pages)
