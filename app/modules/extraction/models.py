"""The Job record (stored as job.json) and the API's response shape.

A "job" is one document going through extraction. In the local proof of concept a job is
created and finished within the same HTTP request; the record still exists so results can
be fetched again later (GET /v1/extractions/{id}) and inspected on disk.

What is deliberately NOT stored: the original file name (it can contain a person's name,
e.g. "aadhaar_rahul.jpg"), and any document content in the job record. Content lives only
in input.* and result.json.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

JobStatus = Literal["processing", "succeeded", "failed"]


class JobError(BaseModel):
    code: str  # stable, for programs (docs/processing-flow.md section 6)
    message: str  # for people; never contains document values


class Job(BaseModel):
    id: str
    document_type: str
    schema_version: str
    status: JobStatus
    file_kind: str  # pdf, docx, jpeg, png: detected from the bytes, not the name
    file_size: int
    sha256: str  # fingerprint of the file, used later to detect re-uploads
    pages: int | None = None
    route: str | None = None  # which path was taken, e.g. "Digital PDF: text only"
    warnings: list[str] = Field(default_factory=list)  # consistency checks, notes
    error: JobError | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Where the time went, in milliseconds. Useful to see whether reading the file or the
    # model is slow.
    prepare_ms: int | None = None
    model_ms: int | None = None
    total_ms: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class JobOut(Job):
    """What the API returns: the job record plus the extracted result when there is one."""

    result: dict | None = None


class DocumentTypeOut(BaseModel):
    name: str
    version: str
    description: str
    json_schema: dict
