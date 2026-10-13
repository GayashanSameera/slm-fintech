"""Errors that stop one document from being extracted.

Each has a stable `code` (programs branch on it) and a `message` for people. Messages
never include document values; at most a field name or a count.

Codes and where they come from (docs/processing-flow.md section 6):

    unsupported_file_type   not a PDF, DOCX, JPG or PNG; or a DOCX with macros
    file_too_large          over the size limit, or an image over the pixel limit
    corrupt_file            the file cannot be opened
    encrypted_pdf           the PDF has a password
    too_many_pages          more pages than allowed
    model_unavailable       the AI model server cannot be reached or timed out
    model_error             the model server refused the request
    model_output_invalid    the model's answer is not usable JSON (or was cut off)
    validation_failed       the answer does not fit the document type's schema

`http_status` is used when the error is raised before a job exists (bad upload). Once a
job exists, the error is recorded on the job instead (status "failed").
"""


class ExtractionError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
