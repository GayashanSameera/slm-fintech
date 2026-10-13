"""Detect the real file type from its first bytes ("magic numbers").

A file name or the browser's Content-Type can say anything ("invoice.pdf" might be a
program). The first bytes of a file are set by its format and are much harder to fake:

    PDF    "%PDF-" within the first 1024 bytes
    PNG    89 50 4E 47 0D 0A 1A 0A
    JPEG   FF D8 FF
    DOCX   a ZIP archive: 50 4B 03 04 ("PK"). docx.py then checks it really is a Word
           document (contains word/document.xml) and has no macros.

Anything else is rejected with "unsupported_file_type".
"""

from enum import StrEnum

from app.modules.extraction.errors import ExtractionError


class FileKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    JPEG = "jpeg"
    PNG = "png"

    @property
    def extension(self) -> str:
        """File extension used when saving the upload (input.pdf, input.jpg, …)."""
        return {"jpeg": "jpg"}.get(self.value, self.value)


def detect_kind(data: bytes) -> FileKind:
    if b"%PDF-" in data[:1024]:
        return FileKind.PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileKind.PNG
    if data.startswith(b"\xff\xd8\xff"):
        return FileKind.JPEG
    if data.startswith(b"PK\x03\x04"):
        return FileKind.DOCX
    raise ExtractionError(
        "unsupported_file_type",
        "Supported file types are PDF, DOCX, JPG and PNG. This file is none of them.",
        http_status=415,
    )
