"""Ollama adapter for LOCAL development (no AWS account, no GPU needed).

Ollama runs a small vision model (default qwen3-vl:4b) on the laptop's CPU.
It exposes an OpenAI-compatible chat API, which this adapter calls.

Whatever model id the ladder asks for (Nova, Haiku, Sonnet), locally we always
use the one Ollama model: the point is to exercise the code path, not to
measure accuracy. Use SLM_MODEL_PROVIDER=bedrock in the dev AWS account for
real accuracy numbers.
"""

from __future__ import annotations

import base64

import httpx

from app.config import settings
from app.core.errors import TemporaryError

from .base import GatewayResult


class OllamaGateway:
    # One local model on a laptop: running chunks in parallel would only queue them up.
    parallel_safe = False

    def generate(self, *, model_id, system, pages, instruction, thinking=False, max_tokens=8000):
        """Same contract as BedrockGateway.generate, for a local Ollama model (developer laptop only).

        Page images go in the message's `images`, text layers and instruction in its text. No retries
        or token caching here: this gateway is for trying prompts locally, never for production.
        """
        content: list[dict] = []
        for number, page in enumerate(pages, start=1):
            content.append({"type": "text", "text": f"<page number='{number}'>"})
            if page.get("image"):
                b64 = base64.b64encode(page["image"]).decode()
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            if page.get("text"):
                content.append({"type": "text", "text": f"<text_layer>\n{page['text']}\n</text_layer>"})
            content.append({"type": "text", "text": "</page>"})
        content.append({"type": "text", "text": instruction})

        body = {
            "model": settings.ollama_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        try:
            # CPU inference is slow: allow up to 15 minutes locally.
            resp = httpx.post(f"{settings.ollama_url}/v1/chat/completions", json=body, timeout=900)
            resp.raise_for_status()
        except httpx.HTTPError:
            raise TemporaryError() from None
        data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return GatewayResult(
            text=choice["message"].get("content") or "",
            model_id=f"ollama:{settings.ollama_model}",
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            stop_reason="max_tokens" if choice.get("finish_reason") == "length" else "end_turn",
        )
