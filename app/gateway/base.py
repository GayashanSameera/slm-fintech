"""The model gateway interface.

The rest of the platform NEVER calls an AI provider directly. It calls
`gateway.generate(...)`, and the gateway decides how to talk to:
  * Amazon Bedrock (AWS)        -> app/gateway/bedrock.py
  * Ollama (developer laptop)   -> app/gateway/ollama.py
  * a fake (unit tests)         -> app/gateway/fake.py

Benefits:
  * One place for retries, token metering, and the "approved models only" rule.
  * The chat module (later) reuses the same gateway (requirement FR-8.2).
  * Tests run without any model at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class GatewayResult:
    text: str                  # raw model output. UNTRUSTED. Never log it.
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str = "end_turn"   # "end_turn" (finished) | "max_tokens" (cut off) | ...


class ModelGateway(Protocol):
    def generate(
        self,
        *,
        model_id: str,           # e.g. "us.anthropic.claude-haiku-5-5"
        system: str,             # fixed instructions (cached by Bedrock)
        pages: list[dict],       # page dicts from preprocessing: {"text", "image"}
        instruction: str,        # the per-call request, e.g. "Extract ..." + problems to fix
        thinking: bool = False,  # Claude only: think before answering (used on retries)
        max_tokens: int = 8000,
    ) -> GatewayResult:
        """Ask a model about the pages; return its raw text answer and token counts."""
        ...
