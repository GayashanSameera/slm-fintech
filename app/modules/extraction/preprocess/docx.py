"""DOCX path, using python-docx (MIT licence).

A .docx file is a ZIP archive of XML files. Its text is real text, so no OCR is needed.
(We chose python-docx over converting with LibreOffice: ~1 MB instead of ~500 MB, and
milliseconds instead of seconds.)

Steps:
    1. Safety: must contain word/document.xml (a real Word file, not another ZIP renamed)
       and must NOT contain word/vbaProject.bin (macros, a common malware carrier).
    2. Read headers, then the body in document order:
           paragraphs → lines of text
           tables     → Markdown tables ("| a | b |"), which models read well
       then footers. Company names and dates often sit in headers/footers.
    3. Pictures inside the document (e.g. a scanned ID pasted into Word) go through the
       image path and are sent as images.
    4. DOCX has no fixed pages, so the page count is estimated: 1 page per 3,000 characters
       plus 1 per picture (the billing rule in processing-flow.md §3.3).
"""

import io
import zipfile

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.config import Settings
from app.modules.extraction.errors import ExtractionError
from app.modules.extraction.preprocess.detect import FileKind
from app.modules.extraction.preprocess.image import image_bytes_to_png

CHARS_PER_PAGE = 3000


def _check_zip(data: bytes) -> None:
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except zipfile.BadZipFile as exc:
        raise ExtractionError("corrupt_file", "The file looks like a ZIP/DOCX but cannot be opened.") from exc
    if "word/document.xml" not in names:
        raise ExtractionError("unsupported_file_type", "This ZIP file is not a Word document.", http_status=415)
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise ExtractionError("unsupported_file_type", "Word files with macros are not accepted.", http_status=415)


def _clean(text: str) -> str:
    return " ".join(text.split())


def _table_lines(table: Table) -> list[str]:
    lines = []
    for r, row in enumerate(table.rows):
        cells = [_clean(c.text).replace("|", "/") for c in row.cells]
        lines.append("| " + " | ".join(cells) + " |")
        if r == 0:  # Markdown header separator after the first row
            lines.append("|" + "|".join(" --- " for _ in cells) + "|")
    return lines


def _paragraph_lines(paragraphs) -> list[str]:
    return [t for t in (_clean(p.text) for p in paragraphs) if t]


def prepare_docx(data: bytes, settings: Settings):
    from app.modules.extraction.preprocess import PreparedDocument

    _check_zip(data)
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises several types for damaged files
        raise ExtractionError("corrupt_file", "The Word document could not be read.") from exc

    lines: list[str] = []
    # Headers first (letterheads, company names), skipping repeats across sections.
    seen_headers: set[str] = set()
    for section in doc.sections:
        for line in _paragraph_lines(section.header.paragraphs):
            if line not in seen_headers:
                seen_headers.add(line)
                lines.append(line)

    # Body: walk the raw XML children so paragraphs and tables stay in document order.
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]  # strip the XML namespace: "{...}p" -> "p"
        if tag == "p":
            text = _clean(Paragraph(child, doc).text)
            if text:
                lines.append(text)
        elif tag == "tbl":
            lines.extend(_table_lines(Table(child, doc)))
            lines.append("")

    seen_footers: set[str] = set()
    for section in doc.sections:
        for line in _paragraph_lines(section.footer.paragraphs):
            if line not in seen_footers:
                seen_footers.add(line)
                lines.append(line)

    text = "\n".join(lines).strip()

    # Embedded pictures live in the document's "related parts" with an image content type.
    images: list[bytes] = []
    notes: list[str] = []
    for rel in doc.part.rels.values():
        if len(images) >= settings.max_pages:
            notes.append(f"Only the first {settings.max_pages} pictures in the document were used.")
            break
        if "image" not in rel.reltype or rel.is_external:
            continue
        try:
            png, _ = image_bytes_to_png(rel.target_part.blob, settings)
            images.append(png)
        except ExtractionError:
            # e.g. old Windows EMF/WMF drawings that Pillow cannot read: skip, do not fail.
            notes.append("A picture in the document could not be read and was skipped.")

    pages = max(1, -(-len(text) // CHARS_PER_PAGE)) + len(images)  # -(-a // b) = ceiling division
    route = "DOCX: text and tables" + (f" plus {len(images)} embedded picture(s)" if images else "")
    return PreparedDocument(kind=FileKind.DOCX, pages=pages, text=text or None, images=images, route=route, notes=notes)
