"""Detect the REAL file type from its first bytes ("magic bytes").

We never trust the file name or the Content-Type header: both are chosen by the
sender and easy to fake (requirement FR-2.3). A file called "invoice.pdf" that
actually contains a PNG is treated as a PNG; random bytes are rejected.
"""

from __future__ import annotations

import io
import zipfile

from app.core.errors import ApiError

SUPPORTED = ("pdf", "docx", "jpeg", "png")


def detect_type(data: bytes) -> str:
    """Return 'pdf' | 'docx' | 'jpeg' | 'png', or raise ApiError(415)."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"PK\x03\x04"):
        # DOCX is a ZIP archive that contains word/document.xml. Other ZIPs are rejected.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            names = set()
        if "word/document.xml" in names:
            return "docx"
    raise ApiError(415, "unsupported_file_type", "Supported file types: PDF, DOCX, JPG, PNG.")


def content_type_for(kind: str) -> str:
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }[kind]
