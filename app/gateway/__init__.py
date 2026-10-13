"""Model gateway: the ONLY place that talks to an AI model (coding rule 5).

    base.py            the interface: ExtractionRequest in, ModelResponse out, and errors
    openai_compat.py   real models through the OpenAI-compatible chat API:
                         Ollama on your machine (or any server with the same API)
    fake.py            canned answers for tests (no model needed)

Because everything goes through this interface, changing the model or the server
(4B → 8B, another server, a fine-tuned version) needs no change elsewhere.
"""

from app.config import Settings
from app.gateway.base import ModelProvider


def build_provider(settings: Settings) -> ModelProvider:
    if settings.model_provider == "fake":
        from app.gateway.fake import FakeProvider

        return FakeProvider()
    from app.gateway.openai_compat import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        base_url=settings.model_base_url,
        model=settings.model_name,
        timeout_s=settings.model_timeout_s,
        max_tokens=settings.model_max_tokens,
        api_key=settings.model_api_key,
    )
