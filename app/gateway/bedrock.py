"""Amazon Bedrock adapter, using the Converse API.

Why Converse? It is ONE request format for every Bedrock model, so the model
ladder (Nova Lite -> Claude Haiku 5.5 -> Claude Sonnet 5.5) can switch models
by changing only the model ID (architecture §6.4).

How the request is built:
    system   = [our fixed instructions, cachePoint]
               The cachePoint marks everything before it as cacheable: Bedrock
               bills repeated cached input at ~10% of the normal price.
    messages = one user message containing, for every page:
                 "<page number='1'>", the page image, the text layer, "</page>"
               followed by the instruction.
               Clear delimiters help the model treat document text as DATA,
               not as instructions (prompt-injection defence, security.md §7.2).

Security:
  * Authenticated with the Lambda's IAM role (SigV4). No API keys.
  * The worker role may only invoke the approved `us.` profiles (IAM policy in
    infra/cloudformation/app.yaml); anything else fails with AccessDenied.
  * Bedrock keeps nothing (data-retention mode `none`, security.md §5).

TO CONFIRM IN THE PHASE-0 SPIKE (architecture §6.4):
  * the exact `thinking` field Claude 5.5 accepts through Converse,
  * whether Nova Lite accepts `cachePoint` in `system` (we omit it for Nova to be safe).
"""

from __future__ import annotations

from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings
from app.core.errors import TemporaryError
from app.storage.aws import platform_session

from .base import GatewayResult

APPROVED_PREFIXES = ("us.amazon.nova-lite", "us.anthropic.claude-haiku-5-5", "us.anthropic.claude-sonnet-5-5")

# Bedrock errors worth retrying later (the job goes back to SQS).
_RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "InternalServerException",
              "ModelTimeoutException", "ModelNotReadyException"}


class BedrockGateway:
    def __init__(self) -> None:
        self.client = platform_session().client(
            "bedrock-runtime",
            region_name=settings.region,
            # read_timeout: a long statement can take a while to generate.
            config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, read_timeout=110),
        )

    def generate(self, *, model_id, system, pages, instruction, thinking=False, max_tokens=8000):
        # Belt and braces: IAM already blocks unapproved models, but fail fast with a clear error.
        if not model_id.startswith(APPROVED_PREFIXES):
            raise ValueError("model not approved")

        content: list[dict] = []
        for number, page in enumerate(pages, start=1):
            content.append({"text": f"<page number='{number}'>"})
            if page.get("image"):
                content.append({"image": {"format": "jpeg", "source": {"bytes": page["image"]}}})
            if page.get("text"):
                content.append({"text": f"<text_layer>\n{page['text']}\n</text_layer>"})
            content.append({"text": "</page>"})
        content.append({"text": instruction})

        system_blocks: list[dict] = [{"text": system}]
        if "anthropic" in model_id:
            system_blocks.append({"cachePoint": {"type": "default"}})

        extra: dict = {}
        if "anthropic" in model_id:
            # Thinking off for normal extraction (saves output tokens); on for retries.
            extra["thinking"] = {"type": "adaptive"} if thinking else {"type": "disabled"}

        kwargs = dict(
            modelId=model_id,
            system=system_blocks,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": max_tokens},
        )
        if extra:
            kwargs["additionalModelRequestFields"] = extra

        try:
            resp = self.client.converse(**kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] in _RETRYABLE:
                raise TemporaryError() from None
            raise

        usage = resp.get("usage", {})
        blocks = resp["output"]["message"]["content"]
        return GatewayResult(
            # Thinking/reasoning blocks have no "text" key; we only keep the answer text.
            text="".join(b.get("text", "") for b in blocks),
            model_id=model_id,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            cache_read_tokens=usage.get("cacheReadInputTokens", 0),
            stop_reason=resp.get("stopReason", "end_turn"),
        )
