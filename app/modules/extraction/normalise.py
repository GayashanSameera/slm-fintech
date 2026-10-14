"""Final shaping of a validated result before masking and storage.

The models are told to LEAVE OUT fields they cannot find (saves output tokens,
which cost 5x input tokens). Requirement FR-3.6 says missing fields are returned
as null. `model_dump()` on a Pydantic model includes every field, with None for
missing ones, so dumping the validated model gives us exactly that.

Dates become "YYYY-MM-DD" strings and amounts are already 2-decimal strings
(see schemas/base.py).
"""

from __future__ import annotations

from pydantic import BaseModel


def to_result(parsed: BaseModel) -> dict:
    # mode="json" applies our serializers (dates -> ISO strings).
    """The validated model as plain JSON: every field present (null when missing), dates as
    YYYY-MM-DD, amounts as 2-decimal strings.
    """
    return parsed.model_dump(mode="json")
