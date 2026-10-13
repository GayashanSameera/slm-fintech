"""Model gateway interface.

ExtractionRequest   what we send: instructions, document text, page images, the schema
ModelResponse       what we get back: the parsed JSON plus usage figures

Errors, split by what the caller should do:
    ModelUnavailable     server down, timeout, overloaded: TEMPORARY, worth retrying later
    ModelError           the server refused the request (bad input): PERMANENT
    ModelOutputInvalid   the answer is unusable (not JSON, or cut off): PERMANENT for this input
"""

from dataclasses import dataclass, field
from typing import Protocol


class ModelUnavailable(Exception):
    pass


class ModelError(Exception):
    pass


class ModelOutputInvalid(Exception):
    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw  # the model's raw answer; for debugging only, never logged


@dataclass
class ExtractionRequest:
    system_prompt: str
    user_prompt: str
    images: list[bytes]  # PNG bytes, in page order
    json_schema: dict  # the output must match this
    adapter: str | None = None  # name of a fine-tuned adapter on the model server (not used yet)


@dataclass
class ModelResponse:
    data: dict
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    extra: dict = field(default_factory=dict)


class ModelProvider(Protocol):
    def extract(self, request: ExtractionRequest) -> ModelResponse: ...
