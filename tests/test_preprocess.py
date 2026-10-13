"""File type detection and preparation (docs/processing-flow.md section 3)."""

import io
import zipfile

import pypdfium2 as pdfium
import pytest
from PIL import Image

from app.modules.extraction.errors import ExtractionError
from app.modules.extraction.preprocess import FileKind, detect_kind, prepare
from tests.conftest import fixture_bytes


def blank_pdf(pages: int) -> bytes:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(595, 842)  # A4 in points
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


# ---- detection ----


@pytest.mark.parametrize(
    ("name", "kind"),
    [("payslip.pdf", FileKind.PDF), ("payslip.docx", FileKind.DOCX), ("payslip-photo.png", FileKind.PNG)],
)
def test_detects_by_content(name, kind):
    assert detect_kind(fixture_bytes(name)) is kind


def test_detects_jpeg():
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, format="JPEG")
    assert detect_kind(out.getvalue()) is FileKind.JPEG


def test_rejects_unknown_bytes():
    with pytest.raises(ExtractionError) as err:
        detect_kind(b"just some text")
    assert err.value.code == "unsupported_file_type"


# ---- PDF ----


def test_digital_pdf_sends_text_only(settings):
    doc = prepare(fixture_bytes("payslip.pdf"), settings)
    assert doc.images == []
    assert "ACME FINANCE" in doc.text and "--- Page 1 ---" in doc.text
    assert doc.pages == 1 and "Digital PDF" in doc.route


def test_scanned_pdf_sends_page_images(settings):
    doc = prepare(fixture_bytes("payslip-scanned.pdf"), settings)
    assert len(doc.images) == 1
    assert "Scanned" in doc.route
    img = Image.open(io.BytesIO(doc.images[0]))
    assert img.format == "PNG" and max(img.size) == settings.max_image_side


def test_too_many_pages(settings):
    with pytest.raises(ExtractionError) as err:
        prepare(blank_pdf(settings.max_pages + 1), settings)
    assert err.value.code == "too_many_pages"


def test_corrupt_pdf(settings):
    with pytest.raises(ExtractionError) as err:
        prepare(b"%PDF-1.4 not really a pdf", settings)
    assert err.value.code == "corrupt_file"


# ---- DOCX ----


def test_docx_text_and_tables(settings):
    doc = prepare(fixture_bytes("payslip.docx"), settings)
    assert "ACME FINANCE" in doc.text
    assert doc.images == [] and doc.route.startswith("DOCX")


def test_zip_that_is_not_docx(settings):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(ExtractionError) as err:
        prepare(out.getvalue(), settings)
    assert err.value.code == "unsupported_file_type"


def test_docx_with_macros_rejected(settings):
    src = zipfile.ZipFile(io.BytesIO(fixture_bytes("payslip.docx")))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for item in src.infolist():
            z.writestr(item, src.read(item))
        z.writestr("word/vbaProject.bin", b"macro")
    with pytest.raises(ExtractionError) as err:
        prepare(out.getvalue(), settings)
    assert "macros" in err.value.message


# ---- images ----


def test_image_resized_and_exif_removed(settings):
    img = Image.new("RGB", (4000, 3000), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90° clockwise to display
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif)

    doc = prepare(out.getvalue(), settings)
    result = Image.open(io.BytesIO(doc.images[0]))
    assert result.size == (1200, 1600)  # rotated upright, long side 1600
    assert not result.getexif()  # metadata dropped


def test_image_pixel_limit(settings):
    settings.max_image_pixels = 100
    out = io.BytesIO()
    Image.new("RGB", (20, 20)).save(out, format="PNG")
    with pytest.raises(ExtractionError) as err:
        prepare(out.getvalue(), settings)
    assert err.value.code == "file_too_large"
