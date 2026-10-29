"""
Saved plans: reusable plan TEMPLATES, not separate workflows.

A saved plan is an ordinary plan with {{parameter}} placeholders. Filling it in
returns a plain plan, which is then validated and compiled by the same engine
as any other — invoice processing and ML splitting have no code paths of
their own. A placeholder that is the whole string ("{{columns}}") takes the
parameter's JSON value (a list, say); one inside a longer string is replaced
by its text.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

SAVED_DIR = Path(__file__).resolve().parent / "saved"
_WHOLE = re.compile(r"^\{\{([a-z_][a-z0-9_]*)\}\}$")
_INNER = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


class SavedPlanError(ValueError):
    pass


def _load(name: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name or ""):
        raise SavedPlanError(f"no saved plan {name!r}")
    path = SAVED_DIR / f"{name}.json"
    if not path.is_file():
        raise SavedPlanError(f"no saved plan {name!r}; available: {[p['name'] for p in list_saved()]}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_saved() -> list[dict]:
    out = []
    for path in sorted(SAVED_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({"name": data["name"], "description": data["description"],
                    "parameters": data["parameters"]})
    return out


def get_saved(name: str) -> dict:
    return _load(name)


def instantiate(name: str, parameters: dict) -> dict:
    """Fill a saved plan's placeholders; returns the plan (not yet validated)."""
    template = _load(name)
    declared = template["parameters"]
    given = dict(parameters or {})
    missing = sorted(set(declared) - set(given))
    extra = sorted(set(given) - set(declared))
    if missing or extra:
        raise SavedPlanError(f"saved plan {name!r}: missing parameters {missing}, unknown parameters {extra}")

    def fill(node):
        if isinstance(node, dict):
            return {fill(k) if isinstance(k, str) else k: fill(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fill(v) for v in node]
        if isinstance(node, str):
            whole = _WHOLE.match(node)
            if whole:
                return copy.deepcopy(given[whole.group(1)])
            return _INNER.sub(lambda mt: str(given[mt.group(1)]), node)
        return node

    plan = fill(template["plan"])
    left = _INNER.findall(json.dumps(plan))
    if left:
        raise SavedPlanError(f"saved plan {name!r}: unfilled placeholders {sorted(set(left))}")
    return plan
