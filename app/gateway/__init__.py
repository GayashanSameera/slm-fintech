"""Choose the model gateway from settings (SLM_MODEL_PROVIDER)."""

from __future__ import annotations

from functools import lru_cache

from app.config import settings

from .base import GatewayResult, ModelGateway  # noqa: F401  (re-exported)


@lru_cache(maxsize=1)
def get_gateway() -> ModelGateway:
    """The ONE model gateway of this process, chosen by SLM_MODEL_PROVIDER:
    "bedrock" (AWS), "ollama" (a developer laptop) or anything else -> the FakeGateway (tests).
    Cached, so the Bedrock client and its connection pool are created once per Lambda container.
    """
    provider = settings.model_provider
    if provider == "bedrock":
        from .bedrock import BedrockGateway
        return BedrockGateway()
    if provider == "ollama":
        from .ollama import OllamaGateway
        return OllamaGateway()
    from .fake import FakeGateway
    return FakeGateway()
