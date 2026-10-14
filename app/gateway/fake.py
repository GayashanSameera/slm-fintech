"""Fake model gateway for tests.

Tests decide what the "model" answers by queuing responses:

    gw = FakeGateway()
    gw.queue('{"invoice_number": "INV-1", ...}')     # first call returns this
    gw.queue('not json')                             # second call returns this

If nothing is queued, it returns "{}" (an empty, valid JSON object).
Every call is recorded in `gw.calls`, so tests can assert which model was
used on which attempt (e.g. "the second attempt climbed to Haiku").
"""

from __future__ import annotations

from collections import deque

from .base import GatewayResult


class FakeGateway:
    # Answers come from a queue in order, so chunked extraction must call it one chunk at a time.
    parallel_safe = False

    def __init__(self) -> None:
        """No answers queued yet; every call will be recorded in `calls`."""
        self._responses: deque[str] = deque()
        self.calls: list[dict] = []

    def queue(self, *responses: str) -> FakeGateway:
        """Queue the next answers, in order. Returns self, so calls can be chained."""
        self._responses.extend(responses)
        return self

    def generate(self, *, model_id, system, pages, instruction, thinking=False, max_tokens=8000):
        """Record the call and return the next queued answer ("{}" when none is left),
        reporting 100 input and 50 output tokens.
        """
        self.calls.append({"model_id": model_id, "thinking": thinking, "instruction": instruction,
                           "pages": len(pages), "system": system})
        text = self._responses.popleft() if self._responses else "{}"
        return GatewayResult(text=text, model_id=model_id, input_tokens=100, output_tokens=50)
