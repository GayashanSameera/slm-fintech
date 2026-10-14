"""Application settings.

EVERY setting comes from an environment variable whose name starts with ``SLM_``.
For example ``SLM_TABLE=slm-main`` sets ``settings.table``.

Why environment variables?
  * The same code runs on a laptop (docker compose) and in AWS Lambda. Only the
    environment differs (requirement NFR-O2).
  * Nothing environment-specific or secret is hard-coded in the source.

Where the values come from:
  * Locally:  ``.env.local`` (loaded by docker compose / the Makefile).
  * In AWS:   the Lambda function's environment, set by the CloudFormation
              template ``infra/cloudformation/app.yaml``.

Secrets (the API-key pepper, webhook secrets) are NOT normal settings in AWS.
They live in AWS Secrets Manager and are read by ``app.core.secrets``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_prefix="SLM_" means the field `table` is read from SLM_TABLE, etc.
    model_config = SettingsConfigDict(env_prefix="SLM_", extra="ignore")

    # --- General -----------------------------------------------------------------
    env: str = "local"                  # "local" | "dev" | "prod". "local" turns off AWS-only features.
    region: str = "us-east-1"           # AWS region we run in (and call Bedrock from).
    log_level: str = "INFO"

    # --- Storage names -----------------------------------------------------------
    table: str = "slm-main"             # DynamoDB single table (architecture §7.1)
    bucket: str = "slm-docs-local"      # S3 bucket for uploaded files and large results (§7.2)
    queue_interactive: str = "jobs-interactive"   # SQS: jobs a client is waiting for
    queue_background: str = "jobs-background"     # SQS: everything else (backlog)
    queue_webhooks: str = "webhooks"              # SQS: webhook deliveries

    # --- Local-only endpoints ----------------------------------------------------
    # Locally these point at docker containers (DynamoDB Local, MinIO, ElasticMQ).
    # In AWS they stay empty (None) so boto3 talks to the real services.
    dynamodb_endpoint: str | None = None
    s3_endpoint: str | None = None
    s3_public_endpoint: str | None = None  # URL a client OUTSIDE docker uses for presigned uploads
    sqs_endpoint: str | None = None

    # --- Security (AWS only) -----------------------------------------------------
    tenant_data_role_arn: str | None = None   # role assumed per tenant (security.md §4.2)
    kms_documents_key_id: str | None = None   # KMS key that encrypts uploaded files
    pepper_secret_id: str | None = None       # Secrets Manager secret holding the API-key pepper
    api_key_pepper: str = "local-only-pepper-not-secret"  # used ONLY when env == "local"

    # --- Model gateway -----------------------------------------------------------
    model_provider: str = "fake"        # "bedrock" | "ollama" | "fake" (tests)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3-vl:4b"
    profile_name: str = "default"       # which file in app/modules/extraction/profiles/ to load

    # --- Limits (requirements FR-2.1, FR-2.2, FR-2.5) ----------------------------
    max_inline_bytes: int = 4 * 1024 * 1024    # direct flow (multipart or base64)
    max_upload_bytes: int = 25 * 1024 * 1024   # presigned flow
    max_pages: int = 20
    max_wait_seconds: int = 40                 # the "wait for result" ceiling (FR-2.14)
    upload_url_ttl_seconds: int = 900          # presigned upload URL validity: 15 minutes

    # --- Processing window (FR-5) ------------------------------------------------
    # "always" (default) or e.g. "Mon-Fri 09:00-19:00 Asia/Kolkata".
    processing_window: str = "always"

    @property
    def is_local(self) -> bool:
        """True when running on a developer machine (no IAM, no KMS, no GuardDuty)."""
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings once per process. Lambda keeps the process warm between
    invocations, so this runs once per cold start, not on every request."""
    return Settings()


# Convenience alias used across the code base: `from app.config import settings`.
settings = get_settings()
