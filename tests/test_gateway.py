"""The OpenAI-compatible model client, against a mocked HTTP server (no Ollama needed)."""

import json

import httpx
import pytest

from app.gateway.base import ExtractionRequest, ModelError, ModelOutputInvalid, ModelUnavailable
from app.gateway.openai_compat import OpenAICompatibleProvider

REQUEST = ExtractionRequest(
    system_prompt="rules", user_prompt="doc", images=[b"\x89PNG fake"], json_schema={"type": "object"}
)


def provider_with(handler) -> OpenAICompatibleProvider:
    p = OpenAICompatibleProvider(base_url="http://model/v1", model="qwen3-vl:4b", timeout_s=5, max_tokens=100)
    p._http = httpx.Client(base_url="http://model/v1", transport=httpx.MockTransport(handler))
    return p


def answer(content: str, finish_reason="stop", status=200):
    body = {
        "model": "qwen3-vl:4b",
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    return lambda request: httpx.Response(status, json=body)


def test_request_shape_and_success():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return answer('{"a": 1}')(request)

    result = provider_with(handler).extract(REQUEST)
    assert result.data == {"a": 1} and result.input_tokens == 10
    assert seen["temperature"] == 0
    assert seen["response_format"]["type"] == "json_schema"
    image_part = seen["messages"][1]["content"][1]
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_not_json():
    with pytest.raises(ModelOutputInvalid):
        provider_with(answer("hello")).extract(REQUEST)


def test_cut_off_answer():
    with pytest.raises(ModelOutputInvalid):
        provider_with(answer('{"a": ', finish_reason="length")).extract(REQUEST)


def test_server_error_is_temporary():
    with pytest.raises(ModelUnavailable):
        provider_with(answer("", status=503)).extract(REQUEST)


def test_bad_request_is_permanent():
    with pytest.raises(ModelError):
        provider_with(lambda r: httpx.Response(404, text="model not found")).extract(REQUEST)


def test_connection_refused():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ModelUnavailable):
        provider_with(handler).extract(REQUEST)
