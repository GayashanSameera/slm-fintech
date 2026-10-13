"""Shared objects for the whole app ("dependencies").

One Deps object is created when the app starts and stored on `app.state.deps`. Routes and
services reach shared things through it:

    deps.settings          configuration
    deps.job_store         reads and writes data/jobs/<job_id>/
    deps.provider          the AI model (through the model gateway)
    deps.extraction_lock   makes documents go through the model one at a time

Why a container instead of global variables? Tests build a Deps with a temporary data
folder and a fake model and pass it to create_app(deps); nothing global is patched.
"""

import threading
from functools import cached_property
from pathlib import Path

from app.config import Settings, get_settings
from app.gateway import build_provider
from app.gateway.base import ModelProvider
from app.storage.local import JobStore


class Deps:
    def __init__(self, settings: Settings | None = None, provider: ModelProvider | None = None):
        self.settings = settings or get_settings()
        self._provider = provider
        self.extraction_lock = threading.Lock()

    @cached_property
    def provider(self) -> ModelProvider:
        return self._provider or build_provider(self.settings)

    @cached_property
    def data_dir(self) -> Path:
        return Path(self.settings.data_dir).resolve()

    @cached_property
    def job_store(self) -> JobStore:
        return JobStore(self.data_dir)
