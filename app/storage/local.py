"""Job storage: plain files under SLM_DATA_DIR (default ./data).

Folder layout, one folder per uploaded document:

    data/jobs/<job_id>/
        input.pdf      the uploaded document (extension from its REAL type: pdf, docx, jpg, png)
        result.json    the extracted JSON (already masked)
        job.json       status, pages, warnings, timings, errors

Open any of these files to see exactly what was stored.

Two safety rules in this file:

1. PATH SAFETY. Job ids come from URLs (GET /v1/extractions/{job_id}). A malicious id like
   "../../etc/passwd" could escape the data folder. Every id is checked against a strict
   pattern BEFORE it is used in a path; anything else is treated as "not found".

2. ATOMIC WRITES. Files are written to a temporary name and then renamed. A rename is
   all-or-nothing, so a crash mid-write never leaves a half-written JSON file behind.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from app.modules.extraction.models import Job

# Allowed shapes. Lowercase letters and digits only: no dots, no slashes.
_JOB_ID = re.compile(r"^job_[0-9a-z]{26}$")
_EXTENSION = re.compile(r"^[a-z0-9]{1,5}$")


class InvalidId(ValueError):
    """An id that does not match its expected pattern. Treated as "not found" by callers."""


def _check(pattern: re.Pattern, value: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidId(value)
    return value


def _write_atomic(path: Path, data: bytes) -> None:
    """Writes `data` to `path` all-or-nothing (temp file in the same folder, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)  # atomic on the same filesystem
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_json(path: Path, obj) -> None:
    _write_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False, default=str).encode("utf-8"))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


class JobStore:
    def __init__(self, data_dir: Path):
        self._jobs = data_dir / "jobs"

    def _dir(self, job_id: str) -> Path:
        return self._jobs / _check(_JOB_ID, job_id)

    def put_job(self, job: Job) -> None:
        _write_json(self._dir(job.id) / "job.json", job.model_dump(mode="json"))

    def get_job(self, job_id: str) -> Job | None:
        try:
            data = _read_json(self._dir(job_id) / "job.json")
        except InvalidId:
            return None  # a malformed id cannot exist, so it is simply "not found"
        return Job.model_validate(data) if data else None

    def list_jobs(self, limit: int) -> list[Job]:
        if not self._jobs.exists():
            return []
        # Job ids are ULIDs, which sort by creation time. Reverse = newest first.
        names = sorted((p.name for p in self._jobs.iterdir() if _JOB_ID.fullmatch(p.name)), reverse=True)
        jobs = []
        for name in names[:limit]:
            job = self.get_job(name)
            if job:
                jobs.append(job)
        return jobs

    def delete_job(self, job_id: str) -> bool:
        """Removes the job folder: document, result and record."""
        try:
            folder = self._dir(job_id)
        except InvalidId:
            return False
        if not folder.exists():
            return False
        shutil.rmtree(folder)
        return True

    def put_input(self, job_id: str, data: bytes, extension: str) -> None:
        _write_atomic(self._dir(job_id) / f"input.{_check(_EXTENSION, extension)}", data)

    def put_result(self, job_id: str, result: dict) -> None:
        _write_json(self._dir(job_id) / "result.json", result)

    def get_result(self, job_id: str) -> dict | None:
        try:
            return _read_json(self._dir(job_id) / "result.json")
        except InvalidId:
            return None
