"""Application settings.

Every setting can be changed with an environment variable named SLM_<FIELD NAME>, for
example SLM_DATA_DIR=/tmp/slm-data. Locally these come from the .env file (copy
.env.example).

How the local proof of concept runs:
    Everything on one machine: FastAPI + the AI model in Ollama. Documents and results are
    plain files under SLM_DATA_DIR. No queue, no login: an upload is processed straight
    away and the JSON comes back in the same response.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_prefix: field `data_dir` is read from SLM_DATA_DIR.
    # env_file: also read .env in the working directory.
    # extra="ignore": unrelated variables in .env are not an error.
    model_config = SettingsConfigDict(env_prefix="SLM_", env_file=".env", extra="ignore")

    # "local" turns on developer conveniences (API docs page, the test UI at /ui).
    env: str = "local"
    log_level: str = "INFO"

    # ---- Storage ----
    # Where everything is kept. Relative paths are relative to the folder you start the
    # server from (normally the repository root).
    data_dir: str = "./data"

    # ---- AI model (the "model gateway", app/gateway) ----
    # "openai_compatible" speaks the OpenAI chat API, which Ollama offers, so the same code
    # would also work with other servers that offer it. "fake" returns canned answers (tests).
    model_provider: Literal["openai_compatible", "fake"] = "openai_compatible"
    model_base_url: str = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint
    model_name: str = "qwen3-vl:4b"
    model_api_key: str | None = None  # Ollama needs none
    # CPU inference is slow: a scanned page can take minutes. 15 minutes before giving up.
    model_timeout_s: float = 900
    # Upper limit on the length of the model's answer. Long bank statements need room.
    model_max_tokens: int = 8192

    # ---- Document limits (docs/processing-flow.md section 3) ----
    max_upload_bytes: int = 25 * 1024 * 1024  # 25 MB
    max_pages: int = 20
    # Images are shrunk so the longest side is at most this many pixels: enough to read
    # text, and fewer image tokens means faster processing.
    max_image_side: int = 1600
    # Refuse images above this size (decompression-bomb protection).
    max_image_pixels: int = 40_000_000
    # A PDF page with fewer characters than this is treated as scanned (an image).
    min_text_chars_per_page: int = 50

    # ---- Masking (requirements FR-4) ----
    # Sensitive numbers are masked in results (last 4 characters kept). List kinds to show in
    # full, comma-separated, from: pan, account, id_number, phone. Aadhaar is ALWAYS masked.
    unmask_fields: str = ""

    @property
    def unmask_set(self) -> frozenset[str]:
        return frozenset(f.strip() for f in self.unmask_fields.split(",") if f.strip())

    # ---- API behaviour ----
    # Comma-separated browser origins allowed to call the API from another port, e.g. the
    # test UI when served separately on :8090. Not needed when the UI is served at /ui.
    cors_origins: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache
def get_settings() -> Settings:
    """Returns one shared Settings object (read from the environment the first time)."""
    return Settings()
