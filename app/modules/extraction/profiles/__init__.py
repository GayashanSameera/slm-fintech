"""Load a model profile (YAML) into typed Python objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Rung:
    model: str
    thinking: bool = False


@dataclass(frozen=True)
class Profile:
    id: str
    prompt_version: int
    ladder: list[Rung]
    max_climbs: int
    think_on_retry: bool
    start_rung: dict[str, int]
    classifier_models: list[str]
    classifier_min_confidence: float
    rung0_enabled: bool
    rung0_verifier: str
    rung0_fallback_rung: int
    extra: dict = field(default_factory=dict)


@lru_cache(maxsize=8)
def load_profile(name: str = "default") -> Profile:
    # Profile names are plain identifiers; never let a setting point outside this folder.
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid profile name")
    raw = yaml.safe_load((_DIR / f"{name}.yaml").read_text())
    return Profile(
        id=raw["id"],
        prompt_version=int(raw["prompt_version"]),
        ladder=[Rung(**r) for r in raw["ladder"]],
        max_climbs=int(raw["max_climbs"]),
        think_on_retry=bool(raw.get("think_on_retry", True)),
        start_rung=dict(raw["start_rung"]),
        classifier_models=list(raw["classifier"]["models"]),
        classifier_min_confidence=float(raw["classifier"]["min_confidence"]),
        rung0_enabled=bool(raw["rung0"]["enabled"]),
        rung0_verifier=raw["rung0"]["verifier"],
        rung0_fallback_rung=int(raw["rung0"]["fallback_rung"]),
    )
