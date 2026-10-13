"""Preprocessing: turn any supported file into what the AI model can read.

The model reads only TEXT and IMAGES. Every file becomes a PreparedDocument:

    file bytes ──► detect.py   what is it really? (from the bytes, not the name)
               ──► pdf.py      digital PDF → text;  scanned PDF → page images (+ any text)
               ──► docx.py     Word → text and tables (+ pictures inside it as images)
               ──► image.py    JPG/PNG → one cleaned, resized image
               ──► PreparedDocument(text, images, pages, route)

docs/processing-flow.md section 3 has a diagram per file type.
"""

from dataclasses import dataclass, field

from app.config import Settings
from app.modules.extraction.preprocess.detect import FileKind, detect_kind


@dataclass
class PreparedDocument:
    kind: FileKind
    pages: int  # billed pages (for DOCX an estimate, see docx.py)
    text: str | None  # sent inside <document>…</document>; None when there is no text layer
    images: list[bytes]  # PNG bytes, one per page/picture, in order
    route: str  # one sentence describing the path taken (stored on the job)
    notes: list[str] = field(default_factory=list)  # things worth telling the user


def prepare(data: bytes, settings: Settings) -> PreparedDocument:
    """Detects the file type and runs the matching preparation step."""
    # Imported here so each module loads its library (pdfium, python-docx, Pillow) only when used.
    kind = detect_kind(data)
    if kind is FileKind.PDF:
        from app.modules.extraction.preprocess.pdf import prepare_pdf

        return prepare_pdf(data, settings)
    if kind is FileKind.DOCX:
        from app.modules.extraction.preprocess.docx import prepare_docx

        return prepare_docx(data, settings)
    from app.modules.extraction.preprocess.image import prepare_image_file

    return prepare_image_file(data, kind, settings)


__all__ = ["FileKind", "PreparedDocument", "detect_kind", "prepare"]
