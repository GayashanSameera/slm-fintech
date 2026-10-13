"""PDF path, using pypdfium2 (Google's PDFium engine; Apache/BSD licence).

We do not use PyMuPDF: it is AGPL-licensed, which could force us to publish our source code
(architecture.md decision D8).

A PDF is one of:
    DIGITAL  made by software (e.g. a bank's statement system). Text is stored as real
             characters, so we read it exactly and send TEXT ONLY: fast, and no reading errors.
    SCANNED  a scan or photo saved as PDF. The "text" is only pixels. We render each page to
             an image and the vision model reads it (the model does the OCR itself).
    MIXED    some pages of each. Treated like scanned, so no page is skipped, and any text
             found is sent too, as a hint.

Steps:
    1. Open. Password-protected → "encrypted_pdf" (we never ask for passwords).
    2. Page limit (SLM_MAX_PAGES, default 20) → "too_many_pages". Keeps model time bounded.
    3. Read each page's text. Digital = every page has at least SLM_MIN_TEXT_CHARS_PER_PAGE
       (50) non-space characters.
    4. If not digital: render each page so its longest side is SLM_MAX_IMAGE_SIDE pixels.
"""

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from app.config import Settings
from app.modules.extraction.errors import ExtractionError
from app.modules.extraction.preprocess.detect import FileKind
from app.modules.extraction.preprocess.image import to_model_png


def _open(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == pdfium_raw.FPDF_ERR_PASSWORD:
            raise ExtractionError(
                "encrypted_pdf", "The PDF is password protected. Remove the password and upload it again."
            ) from exc
        raise ExtractionError("corrupt_file", "The PDF could not be opened.") from exc


def _page_text(page: pdfium.PdfPage) -> str:
    textpage = page.get_textpage()
    try:
        # PDFium uses Windows line endings; normalise them.
        return textpage.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n").strip()
    finally:
        textpage.close()


def prepare_pdf(data: bytes, settings: Settings):
    from app.modules.extraction.preprocess import PreparedDocument

    pdf = _open(data)
    try:
        page_count = len(pdf)
        if page_count == 0:
            raise ExtractionError("corrupt_file", "The PDF has no pages.")
        if page_count > settings.max_pages:
            raise ExtractionError(
                "too_many_pages", f"The PDF has {page_count} pages; the limit is {settings.max_pages}."
            )

        texts = []
        for i in range(page_count):
            page = pdf[i]
            try:
                texts.append(_page_text(page))
            finally:
                page.close()

        # Count characters without spaces, so a page of whitespace does not count as text.
        digital = all(len("".join(t.split())) >= settings.min_text_chars_per_page for t in texts)
        # "--- Page 2 ---" markers tell the model where each page starts.
        text = "\n\n".join(f"--- Page {i + 1} ---\n{t}" for i, t in enumerate(texts)) if any(texts) else None

        images: list[bytes] = []
        if not digital:
            for i in range(page_count):
                page = pdf[i]
                try:
                    width, height = page.get_size()  # in PDF points (1/72 inch)
                    # Pick the zoom that makes the longest side exactly max_image_side pixels.
                    scale = settings.max_image_side / max(width, height)
                    bitmap = page.render(scale=scale)
                    images.append(to_model_png(bitmap.to_pil(), settings))
                finally:
                    page.close()

        plural = "s" if page_count > 1 else ""
        return PreparedDocument(
            kind=FileKind.PDF,
            pages=page_count,
            text=text,
            images=images,
            route=(
                f"Digital PDF, {page_count} page{plural}: text only"
                if digital
                else f"Scanned or mixed PDF, {page_count} page{plural}: page images plus any text found"
            ),
        )
    finally:
        pdf.close()  # free PDFium's memory even when we stop with an error
