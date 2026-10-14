"""Unit tests for real file-type detection from magic bytes (FR-2.3, guide §6.1).

The file name and Content-Type header are chosen by the sender, so they are
never trusted. These tests build each supported type in memory (synthetic
content only) and check that detection looks at the BYTES.
"""

from __future__ import annotations

import io
import os
import zipfile

import pytest
from docx import Document
from PIL import Image

from app.core.errors import ApiError, JobError
from app.modules.extraction.preprocess import preprocess
from app.modules.extraction.preprocess.detect import detect_type
from tests.helpers.simple_pdf import make_pdf


def _image_bytes(fmt: str) -> bytes:
    """A small white image encoded as `fmt` ("PNG" or "JPEG")."""
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format=fmt)
    return buf.getvalue()


def _docx_bytes() -> bytes:
    """A minimal real DOCX file with one paragraph."""
    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("Synthetic payslip for tests")
    doc.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("data, expected", [
    (make_pdf([[(40, 800, "hello")]]), "pdf"),
    (_image_bytes("PNG"), "png"),
    (_image_bytes("JPEG"), "jpeg"),
    (_docx_bytes(), "docx"),
])
def test_each_real_type_is_detected(data, expected):
    assert detect_type(data) == expected


def test_pdf_name_with_png_content_is_png():
    # A client uploads "invoice.pdf", but the bytes are a PNG. We go by the bytes.
    # (detect_type never even sees the file name: that is the point.)
    assert detect_type(_image_bytes("PNG")) == "png"


@pytest.mark.parametrize("data", [
    os.urandom(256),                      # random bytes
    b"",                                  # empty file
    b"GIF89a" + b"\0" * 20,               # an image format we don't support
])
def test_unsupported_bytes_are_415(data):
    with pytest.raises(ApiError) as exc:
        detect_type(data)
    assert exc.value.status == 415
    assert exc.value.code == "unsupported_file_type"


def test_zip_that_is_not_docx_is_415():
    # DOCX files are ZIP archives, but not every ZIP is a DOCX: we look for word/document.xml.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "not a word document")
    with pytest.raises(ApiError) as exc:
        detect_type(buf.getvalue())
    assert exc.value.status == 415


def test_worker_side_bad_file_becomes_job_error():
    # In the worker there is no HTTP request: preprocess() turns the 415 into a failed JOB.
    with pytest.raises(JobError) as exc:
        preprocess(os.urandom(64))
    assert exc.value.code == "unsupported_file_type"
