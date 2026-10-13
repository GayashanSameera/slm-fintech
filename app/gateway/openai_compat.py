"""Talks to a model server through the OpenAI-compatible chat API.

Ollama offers this API at http://localhost:11434/v1. Other model servers (e.g. vLLM on a
GPU machine) offer the same API, so this class would work with them unchanged.

The request, explained:

    POST {base_url}/chat/completions
    {
      "model": "qwen3-vl:4b",          which model
      "temperature": 0,                always the most likely answer: repeatable, not creative
      "max_tokens": 8192,              upper limit on the answer's length
      "messages": [
        {"role": "system", "content": "<rules>"},
        {"role": "user", "content": [
            {"type": "text", "text": "<document type, text layer, schema>"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},  one per page
        ]}
      ],
      "response_format": {             STRUCTURED OUTPUT: the server only lets the model
        "type": "json_schema",         produce JSON matching this schema, so the answer
        "json_schema": {...}           always parses and has the right fields
      }
    }

Context length (how much text + images the model can take in) is set on the Ollama
server (OLLAMA_CONTEXT_LENGTH=16384), because the OpenAI-style API has no field for it.
"""

import base64
import json
import time

import httpx

from app.gateway.base import (
    ExtractionRequest,
    ModelError,
    ModelOutputInvalid,
    ModelResponse,
    ModelUnavailable,
)


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, model: str, timeout_s: float, max_tokens: int, api_key: str | None = None):
        self._model = model
        self._max_tokens = max_tokens
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # One HTTP client, reused for every request (keeps the connection open).
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            # Connecting should be quick; the model's answer can take many minutes on CPU.
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        )

    def extract(self, request: ExtractionRequest) -> ModelResponse:
        content: list[dict] = [{"type": "text", "text": request.user_prompt}]
        for png in request.images:
            encoded = base64.b64encode(png).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})

        body = {
            "model": request.adapter or self._model,
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": request.json_schema},
            },
        }

        started = time.perf_counter()
        try:
            response = self._http.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise ModelUnavailable("The model did not answer in time.") from exc
        except httpx.TransportError as exc:  # connection refused, DNS failure, reset, ...
            raise ModelUnavailable("The model server cannot be reached.") from exc

        if response.status_code >= 500:
            raise ModelUnavailable(f"The model server failed (HTTP {response.status_code}).")
        if response.status_code >= 400:
            # Include the server's short explanation (e.g. "model not found"); it never
            # contains document content.
            detail = response.text[:300]
            raise ModelError(f"The model server refused the request (HTTP {response.status_code}): {detail}")

        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""

        # "length" = the answer hit max_tokens and stopped mid-way: the JSON is incomplete.
        if choice.get("finish_reason") == "length":
            raise ModelOutputInvalid("The model's answer was cut off because it was too long.", raw=text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelOutputInvalid("The model did not return valid JSON.", raw=text) from exc
        if not isinstance(data, dict):
            raise ModelOutputInvalid("The model returned JSON that is not an object.", raw=text)

        usage = payload.get("usage") or {}
        return ModelResponse(
            data=data,
            model=payload.get("model") or body["model"],
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            extra={"seconds": round(time.perf_counter() - started, 2)},
        )
