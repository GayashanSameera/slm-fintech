"""A stand-in model for tests and for trying the API without Ollama (SLM_MODEL_PROVIDER=fake).

It returns a canned answer chosen by the schema's shape, or {} (every field null), and
records the last request so tests can check what WOULD have been sent to a real model.
"""

from app.gateway.base import ExtractionRequest, ModelResponse


class FakeProvider:
    def __init__(self, answers: dict[str, dict] | None = None, error: Exception | None = None):
        # answers: first schema property name -> answer, e.g. {"employer_name": {...payslip...}}
        self.answers = answers or {}
        self.error = error  # set to make every call fail with this exception
        self.last_request: ExtractionRequest | None = None

    def extract(self, request: ExtractionRequest) -> ModelResponse:
        self.last_request = request
        if self.error:
            raise self.error
        first_field = next(iter(request.json_schema.get("properties", {})), "")
        return ModelResponse(
            data=dict(self.answers.get(first_field, {})), model="fake", input_tokens=0, output_tokens=0
        )
