"""Compare an extraction with its known truth, field by field.

The central question of the evaluation is not only "how many fields are right" but

    WHEN A FIELD IS WRONG, DOES THE PLATFORM SAY SO?

A wrong field that comes back with no warning is a WRONG PASS: the dangerous case (the platform's
rule is that nothing may pass wrongly). A wrong field on a document that carries warnings is
FLAGGED: the client knows to look. So we report both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.modules.extraction.crossread import INTERPRETIVE_FIELDS, normalise


def flatten(value, path: str = "") -> dict[str, str]:
    """{path: normalised leaf} for every leaf; lists are indexed, empty leaves are dropped."""
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for key, child in value.items():
            if key in INTERPRETIVE_FIELDS:
                continue                        # wording/enum choices are not "reading errors"
            out.update(flatten(child, f"{path}.{key}" if path else key))
        return out
    if isinstance(value, list):
        out = {}
        for i, child in enumerate(value):
            out.update(flatten(child, f"{path}[{i}]"))
        out[f"{path}#len"] = str(len(value))        # a missing or extra row is an error too
        return out
    text = normalise(value)
    return {path: text} if text else {}


@dataclass
class DocScore:
    case_id: str
    doc_type: str
    condition: str
    fields: int = 0                      # fields in the truth
    correct: int = 0
    wrong: list[str] = field(default_factory=list)       # truth fields read wrongly or missing
    invented: list[str] = field(default_factory=list)    # fields returned that the truth does not have
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    calls: int = 0
    failed: str | None = None            # error code if the job produced no result at all
    by_model: dict = field(default_factory=dict)   # model id -> [calls, cost USD]
    pages: int = 0

    @property
    def has_error(self) -> bool:
        return bool(self.wrong or self.invented or self.failed)

    @property
    def wrongly_passed(self) -> bool:
        """Errors in the result and not a single warning to tell the client."""
        return self.has_error and not self.warnings and not self.failed


def score(case, result: dict | None, warnings: list[str], cost: float, calls: int, failed: str | None = None) -> DocScore:
    """Score one document. `case.truth` and `result` must already be in the same (masked) form."""
    s = DocScore(case.case_id, case.doc_type, case.condition, warnings=warnings, cost_usd=cost, calls=calls, failed=failed)
    truth = flatten(case.truth_final)
    s.fields = len(truth)
    if result is None:
        s.wrong = list(truth)
        return s
    got = flatten(result)
    for path, expected in truth.items():
        if got.get(path) == expected:
            s.correct += 1
        else:
            s.wrong.append(path)
    s.invented = [p for p in got if p not in truth and not p.endswith("#len")]
    if case.doc_type == "bank_statement":           # header fields missing from an answer key are not scored
        s.invented = [p for p in s.invented if p.startswith("transactions")]
    return s
