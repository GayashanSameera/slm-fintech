"""Base class for document schemas, and the schema we hand to the AI model.

Every document type is a Pydantic model (see invoice.py etc.). Pydantic gives us:
    - validation: the model's JSON is checked field by field, and values are normalised
      (dates, amounts) by the types in types.py;
    - a JSON schema, which we send to the model so it knows the exact fields, and which
      Ollama/vLLM use to FORCE the output into that shape ("structured output").

Pydantic's own JSON schema uses features some model servers handle poorly ($ref links to
shared definitions, anyOf for optional fields). `schema_for_model()` rewrites it into a
plain, self-contained form that works reliably for structured output.
"""

import copy
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

# Keys that only document the schema; they mean nothing to the model.
_META_KEYS = {"title", "default"}


class DocumentSchema(BaseModel):
    # extra="ignore": if the model adds a field we did not ask for, drop it silently.
    model_config = ConfigDict(extra="ignore")

    def consistency_warnings(self) -> list[str]:
        """Arithmetic and format checks for this document type (FR-3.9).

        Each subclass overrides this. Results are WARNINGS shown with the result, never
        failures: a mismatch may be a misread digit, or an error in the document itself.
        """
        return []


# ---------- Helpers for the checks ----------

TOLERANCE = Decimal("1.00")  # allow up to ₹1 rounding difference


def differs(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) > TOLERANCE


def total(values) -> Decimal:
    return sum((v for v in values if v is not None), Decimal("0"))


def fmt(amount: Decimal) -> str:
    """12345.5 -> "12,345.50" for readable warning messages."""
    return f"{amount:,.2f}"


# ---------- Schema for the model ----------


def schema_for_model(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema → plain JSON schema for structured output.

    - Inlines every $ref (nested models such as an invoice's seller) so the schema is one tree.
    - Turns "anyOf: [X, null]" into "type: [X, null]" (and adds null to enums).
    - Removes title/default noise.
    - Marks every property as required, so the model always writes every key (null when
      the value is missing). Every result of a type then has the same shape.
    - Forbids extra properties.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node):
        if isinstance(node, list):
            return [resolve(n) for n in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            target = copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]])
            extra = {k: v for k, v in node.items() if k != "$ref"}  # e.g. a description on the field
            return resolve({**target, **extra})

        out = {}
        for key, value in node.items():
            if key in _META_KEYS:
                continue
            if key == "properties":  # a map of FIELD NAMES -> schemas; do not strip names
                out[key] = {name: resolve(sub) for name, sub in value.items()}
            else:
                out[key] = resolve(value)

        if "anyOf" in out:
            options = out["anyOf"]
            non_null = [o for o in options if o.get("type") != "null"]
            if len(non_null) == 1 and len(options) == 2:
                merged = {**{k: v for k, v in out.items() if k != "anyOf"}, **non_null[0]}
                kind = merged.get("type")
                if isinstance(kind, str):
                    merged["type"] = [kind, "null"]
                if "enum" in merged and None not in merged["enum"]:
                    merged["enum"] = [*merged["enum"], None]
                out = merged

        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return resolve(raw)
