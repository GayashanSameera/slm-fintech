"""Merge the four CloudFormation stacks into ONE template for a proof of concept.

    uv run python scripts/build_single_stack.py      # -> infra/cloudformation/build/slm-poc.json

Why: a proof of concept must be easy to remove completely. One stack means one
`delete-stack` removes everything (scripts/poc.py down). The four separate
stacks (data, security, app, observability) stay the source of truth: this
script GENERATES the single stack from them, so the two can never drift apart.
Never edit the generated file; edit the four templates and re-run this.

What the merge does:
  1. Copies Parameters, Conditions, Resources and Outputs of all four templates
     into one document (names don't collide: they were designed to be unique).
  2. Rewires the cross-stack links. Each `Fn::ImportValue "slm-${Env}-X"` is
     replaced by the expression the exporting stack used for its Export X
     (e.g. ImportValue TableArn -> GetAtt Table.Arn). Outputs lose their Export.
  3. Takes the code bucket OUT of the stack and turns it into a parameter
     (CodeBucketName). The Lambda zip must be uploaded BEFORE the functions are
     created, so its bucket can't be created by the same stack. scripts/poc.py
     creates that one bucket itself and deletes it on `down`.
  4. Makes everything deletable:
       * DeletionPolicy / UpdateReplacePolicy Retain -> Delete
       * DynamoDB deletion protection off
       * KMS keys: shortest deletion wait (7 days; AWS never deletes a key at once)
  5. POC defaults: Env=poc, WAF off, no own CloudTrail trail, CORS "*" for the
     browser test page (tools/poc_ui/index.html).

The output is JSON (CloudFormation accepts JSON and YAML equally); comments
live in the four source templates.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "infra" / "cloudformation"
OUT = SRC / "build" / "slm-poc.json"
STACKS = ("data", "security", "app", "observability")

# Parameter defaults for the proof of concept (can still be overridden at deploy time).
POC_DEFAULTS = {"Env": "poc", "EnableWaf": "false", "CreateTrail": "false",
                # the browser test page (tools/poc_ui) calls the API directly
                "CorsAllowOrigin": "*"}


# --- 1. Read CloudFormation YAML (short-form tags like !Ref, !Sub, !GetAtt) ------------------

class _CfnLoader(yaml.SafeLoader):
    """A YAML loader that turns `!Ref X` into {"Ref": "X"}, `!Sub ...` into {"Fn::Sub": ...}, etc."""


def _construct(tag: str):
    name = "Ref" if tag == "Ref" else f"Fn::{tag}"

    def build(loader: yaml.Loader, node: yaml.Node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
            if tag == "GetAtt":                       # "Table.Arn" -> ["Table", "Arn"]
                value = value.split(".", 1)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {name: value}
    return build


for _tag in ("Ref", "Sub", "GetAtt", "If", "Equals", "Not", "And", "Or", "Join", "Select",
             "Split", "FindInMap", "Base64", "Cidr", "ImportValue", "GetAZs", "Condition"):
    _CfnLoader.add_constructor(f"!{_tag}", _construct(_tag))


def load(stack: str) -> dict:
    return yaml.load((SRC / f"{stack}.yaml").read_text(), Loader=_CfnLoader)  # noqa: S506 (SafeLoader subclass)


# --- 2. Helpers -------------------------------------------------------------------------------

def _export_name(output: dict) -> str | None:
    """Export: {Name: !Sub "slm-${Env}-TableArn"} -> "TableArn"."""
    name = (output.get("Export") or {}).get("Name")
    if isinstance(name, dict) and isinstance(name.get("Fn::Sub"), str):
        return name["Fn::Sub"].removeprefix("slm-${Env}-")
    return None


def _import_name(node) -> str | None:
    """{"Fn::ImportValue": {"Fn::Sub": "slm-${Env}-TableArn"}} -> "TableArn"."""
    if isinstance(node, dict) and list(node) == ["Fn::ImportValue"]:
        inner = node["Fn::ImportValue"]
        if isinstance(inner, dict) and isinstance(inner.get("Fn::Sub"), str):
            return inner["Fn::Sub"].removeprefix("slm-${Env}-")
    return None


def rewire(node, exports: dict):
    """Replace every ImportValue with the expression its export stood for."""
    name = _import_name(node)
    if name is not None:
        if name not in exports:
            raise SystemExit(f"ImportValue of unknown export {name!r}")
        return copy.deepcopy(exports[name])
    if isinstance(node, dict):
        return {k: rewire(v, exports) for k, v in node.items()}
    if isinstance(node, list):
        return [rewire(v, exports) for v in node]
    return node


# --- 3. Merge ---------------------------------------------------------------------------------

def build() -> Path:
    merged: dict = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "SLM Fintech - proof of concept, ALL resources in one stack "
                       "(generated by scripts/build_single_stack.py; delete the stack to remove everything)",
        "Parameters": {}, "Conditions": {}, "Resources": {}, "Outputs": {},
    }
    exports: dict = {}
    for stack in STACKS:
        tpl = load(stack)
        for section in ("Parameters", "Conditions", "Resources"):
            for key, value in (tpl.get(section) or {}).items():
                # Shared names (the Env parameter, the EnforceContext condition) may appear in
                # several stacks, but only with IDENTICAL definitions; anything else is a conflict.
                if key in merged[section] and merged[section][key] != value and section != "Parameters":
                    raise SystemExit(f"{section} {key!r} is defined differently in two stacks")
                merged[section].setdefault(key, value)
        for key, output in (tpl.get("Outputs") or {}).items():
            name = _export_name(output)
            if name:
                exports[name] = output["Value"]
            merged["Outputs"][key] = {k: v for k, v in output.items() if k != "Export"}

    # The code bucket can't live in the stack whose functions need its contents.
    merged["Resources"].pop("CodeBucket")
    merged["Outputs"].pop("CodeBucketName")
    merged["Parameters"]["CodeBucketName"] = {
        "Type": "String", "Description": "Bucket holding the Lambda zip (created by scripts/poc.py)."}
    exports["CodeBucketName"] = {"Ref": "CodeBucketName"}

    merged = rewire(merged, exports)

    # Everything must go when the stack is deleted.
    for res in merged["Resources"].values():
        for policy in ("DeletionPolicy", "UpdateReplacePolicy"):
            if res.get(policy) == "Retain":
                res[policy] = "Delete"
        props = res.setdefault("Properties", {})
        if res["Type"] == "AWS::DynamoDB::Table":
            props["DeletionProtectionEnabled"] = False
        if res["Type"] == "AWS::KMS::Key":
            props["PendingWindowInDays"] = 7

    for key, default in POC_DEFAULTS.items():
        if key in merged["Parameters"]:
            merged["Parameters"][key]["Default"] = default

    leftover = json.dumps(merged)
    for marker in ("Fn::ImportValue", '"Retain"'):
        if marker in leftover:
            raise SystemExit(f"generated template still contains {marker}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(merged, indent=1, sort_keys=False) + "\n")
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(merged['Resources'])} resources, "
          f"{len(merged['Parameters'])} parameters, {OUT.stat().st_size / 1024:.0f} KB")
    return OUT


if __name__ == "__main__":
    sys.exit(0 if build() else 1)
