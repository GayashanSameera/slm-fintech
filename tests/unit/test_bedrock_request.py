"""The Bedrock request format, per model, as confirmed on real Bedrock (2026-10-14).

Getting this wrong doesn't show up in any other test (they use the FakeGateway) but
makes every real call fail with ValidationException, as happened on the first AWS test.
"""

from __future__ import annotations

import pytest

from app.gateway.bedrock import build_request

NOVA = "us.amazon.nova-lite-v1:0"
HAIKU = "us.anthropic.claude-haiku-5-5"
SONNET = "us.anthropic.claude-sonnet-5-5"
PAGES = [{"text": "TAX INVOICE", "image": b"\xff\xd8jpeg"}]


def _req(model: str, thinking: bool = False) -> dict:
    """The Converse request the gateway would send for `model`."""
    return build_request(model_id=model, system="rules", pages=PAGES, instruction="Extract.",
                         thinking=thinking, max_tokens=1000)


@pytest.mark.parametrize("model, expected", [
    (HAIKU, {"type": "disabled"}),          # Haiku rejects "between_tools"
    (SONNET, {"type": "between_tools"}),    # Sonnet rejects "disabled"
])
def test_thinking_off_is_model_specific(model, expected):
    assert _req(model)["additionalModelRequestFields"] == {"thinking": expected}


@pytest.mark.parametrize("model", [HAIKU, SONNET])
def test_thinking_on_is_adaptive(model):
    assert _req(model, thinking=True)["additionalModelRequestFields"] == {"thinking": {"type": "adaptive"}}


@pytest.mark.parametrize("model", [HAIKU, SONNET])
def test_claude_gets_no_temperature_but_a_cache_point(model):
    req = _req(model)
    assert "temperature" not in req["inferenceConfig"]          # "deprecated for this model"
    assert req["system"][-1] == {"cachePoint": {"type": "default"}}


def test_nova_gets_temperature_zero_and_no_claude_fields():
    req = _req(NOVA)
    assert req["inferenceConfig"] == {"maxTokens": 1000, "temperature": 0}
    assert "additionalModelRequestFields" not in req
    assert req["system"] == [{"text": "rules"}]


def test_pages_are_wrapped_so_document_text_stays_data():
    content = _req(NOVA)["messages"][0]["content"]
    assert content[0] == {"text": "<page number='1'>"}
    assert content[1]["image"]["format"] == "jpeg"
    assert content[2] == {"text": "<text_layer>\nTAX INVOICE\n</text_layer>"}
    assert content[3] == {"text": "</page>"} and content[-1] == {"text": "Extract."}
