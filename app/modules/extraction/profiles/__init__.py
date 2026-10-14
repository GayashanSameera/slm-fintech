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
    # Bank statements longer than this many pages are read by AI in chunks of this size (pipeline.py).
    statement_chunk_pages: int = 3
    # Independent second reading + 2-of-3 vote for documents without a text proof (crossread.py).
    crossread_enabled: bool = False
    crossread_digital: bool = False          # also read DIGITAL documents twice (text grounding already proves them)
    crossread_readers: tuple[str, ...] = ()
    crossread_arbiters: tuple[str, ...] = ()
    # SCANNED bank statements are read one page per call (measured far more accurate than 3-page chunks)
    # and each page gets its second reading from this model, on an enhanced picture of the page.
    scan_chunk_pages: int = 1
    scan_reader: str | None = None
    scan_second_reading: bool = False     # opt-in: measured about 5x the cost for no measured gain (see the YAML)
    extra: dict = field(default_factory=dict)


@lru_cache(maxsize=8)
def load_profile(name: str = "default") -> Profile:
    # Profile names are plain identifiers; never let a setting point outside this folder.
    """Read profiles/<name>.yaml into a Profile (cached: each profile is read once per process).

    The name is checked to be a plain identifier first, so a setting can never point the
    loader at a file outside this folder.
    """
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
        statement_chunk_pages=int(raw.get("statement_chunk_pages", 3)),
        crossread_enabled=bool(raw.get("crossread", {}).get("enabled", False)),
        scan_chunk_pages=int(raw.get("scan_statements", {}).get("chunk_pages", 1)),
        scan_second_reading=bool(raw.get("scan_statements", {}).get("second_reading", False)),
        scan_reader=raw.get("scan_statements", {}).get("reader"),
        crossread_digital=bool(raw.get("crossread", {}).get("digital", False)),
        crossread_readers=tuple(raw.get("crossread", {}).get("readers", [])),
        crossread_arbiters=tuple(raw.get("crossread", {}).get("arbiters", [])),
    )
