"""
JSON schemas published in the MCP tool definitions.

The model fills a structured object only if the tool's schema describes it.
With a bare `plan: object`, live runs showed the model hand-writing the plan
as one JSON string and getting the brackets wrong in 7 of 9 attempts
(2026-10-23). So the tools advertise the full plan schema, generated from the
same pydantic models that validate it — they cannot drift apart.

The published copy is compacted: $ref/$defs are inlined (not every model API
resolves references), and pydantic's "title" and "discriminator" keys are
dropped (the op/kind constants already say which variant is which). Unknown
fields stay forbidden ("additionalProperties": false).
"""

from __future__ import annotations

import copy
import json

from . import model as m

_DROP = {"title", "discriminator"}


def _inline(node, defs: dict, path: tuple = ()):
    """Replace every $ref by its definition. A definition that refers to itself
    (a condition inside all/any) is inlined once; deeper copies are described
    in words, so the published schema stays finite."""
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            if name in path:
                return {"type": "object",
                        "description": f"a nested {name.lower()} (same shape as the enclosing one)"}
            merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
            return _inline(merged, defs, path + (name,))
        return {k: _inline(v, defs, path) for k, v in node.items() if k not in _DROP and k != "$defs"}
    if isinstance(node, list):
        return [_inline(v, defs, path) for v in node]
    return node


def compact(model_schema: dict) -> dict:
    return _inline(model_schema, model_schema.get("$defs", {}))


def plan_tool_schema() -> dict:
    """Input schema of validate_plan / compile_plan: {plan: <full plan>}."""
    return {"type": "object", "properties": {"plan": compact(m.Plan.model_json_schema())},
            "required": ["plan"], "additionalProperties": False}


def table_plan_tool_schema() -> dict:
    """Input schema of validate_table_plan / compile_table_plan: the flat form's fields at top level."""
    schema = compact(m.TablePlan.model_json_schema())
    schema.pop("description", None)
    return schema


def size(schema: dict) -> int:
    return len(json.dumps(schema, separators=(",", ":")))
