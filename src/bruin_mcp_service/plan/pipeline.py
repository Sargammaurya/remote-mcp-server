"""
Turning a validated plan into a Bruin pipeline on disk.

Layout, per user:
    <workspace>/users/<id>/pipelines/<pipeline_id>/
        .bruin.yml            DuckDB connection → warehouse.duckdb in this folder
        .git/                 `bruin` needs a git root (no commits are made)
        pipeline/pipeline.yml
        pipeline/assets/raw/<table>.sql
        pipeline/assets/clean/<table>.sql
        pipeline/assets/out/<name>.sql
        plan.json             the plan as submitted
        compiled.json         assets, per-step schemas, warnings

Everything is built in a staging folder; `bruin validate` and `bruin render`
must pass there before the folder is renamed into place, so a failed plan
leaves nothing behind and a pipeline folder only ever holds a validated plan.
Nothing is executed here (that is Phase 6).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from .. import datasets
from ..bruin_cli import run_bruin
from ..bruin_query import guarded_query, sql_string
from ..config import Settings
from ..paths import resolve_user_path, user_root
from . import model as m
from .engine import Compiled, PlanError, compile_plan

_PIPELINE_ID = re.compile(r"pl_[0-9a-f]{16}")


class PipelineError(ValueError):
    """Bruin rejected the generated pipeline, or a pipeline id is unknown."""


def read_source_columns(settings: Settings, ds: datasets.Dataset) -> list[str]:
    """Column names exactly as the raw asset will see them (read as text)."""
    described = guarded_query(
        settings, ds.dir,
        f"DESCRIBE SELECT * FROM read_csv({sql_string(str(ds.data_csv))}, header = true, all_varchar = true)")
    return [row[0] for row in described["rows"]]


def resolve_sources(settings: Settings, user_id: str, plan: m.Plan) -> tuple[dict, dict]:
    paths, schemas, problems = {}, {}, []
    for src in plan.sources:
        try:
            ds = datasets.get(settings, user_id, src.dataset_id)
        except datasets.DatasetError as e:
            problems.append(f"source {src.table!r}: {e}")
            continue
        paths[src.table] = str(ds.data_csv)
        schemas[src.table] = read_source_columns(settings, ds)
    if problems:
        raise PlanError(problems)
    return paths, schemas


def describe_step(step) -> str:
    """One line per requested step, so the caller can check nothing was lost."""
    data = step.model_dump(mode="json", exclude_none=True)
    op = data.pop("op")
    return f"{op} " + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in data.items())


def steps_received(plan: m.Plan) -> dict[str, list[str]]:
    return {f"clean.{t.table}": [f"{i}. {describe_step(s)}" for i, s in enumerate(t.steps, 1)]
            for t in plan.clean}


_DRAFT_ID = re.compile(r"dr_[0-9a-f]{16}")
DRAFT_TTL_S = 24 * 3600


def _drafts_root(settings: Settings, user_id: str) -> Path:
    root = user_root(settings.workspace_root, user_id) / "drafts"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def validate(settings: Settings, user_id: str, plan: m.Plan) -> dict:
    """Check a plan against the real data, store it as a draft, write no pipeline.

    The draft is what compile_plan compiles, so the plan is sent once and
    cannot change (or lose steps) between being checked and being built.
    """
    paths, schemas = resolve_sources(settings, user_id, plan)
    compiled = compile_plan(plan, paths, schemas)
    _check_every_step_compiled(plan, compiled)
    draft_id = "dr_" + secrets.token_hex(8)
    path = _drafts_root(settings, user_id) / f"{draft_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"created_at": int(time.time()), "plan": plan.model_dump(mode="json")}),
                   encoding="utf-8")
    os.replace(tmp, path)
    return {"valid": True, "draft_id": draft_id, "plan": plan.name,
            "steps_received": steps_received(plan),
            "next": "check steps_received covers everything the user asked for, then compile_plan(draft_id)",
            **compiled.summary()}


def load_draft(settings: Settings, user_id: str, draft_id: str) -> m.Plan:
    if not isinstance(draft_id, str) or not _DRAFT_ID.fullmatch(draft_id):
        raise PipelineError(f"no draft {draft_id!r}; validate the plan first (validate_table_plan or validate_plan)")
    _drafts_root(settings, user_id)
    path = resolve_user_path(settings.workspace_root, user_id, f"drafts/{draft_id}.json")
    if not path.is_file():
        raise PipelineError(f"no draft {draft_id!r}; validate the plan first (validate_table_plan or validate_plan)")
    data = json.loads(path.read_text(encoding="utf-8"))
    if time.time() - data["created_at"] > DRAFT_TTL_S:
        raise PipelineError(f"draft {draft_id} has expired; validate the plan again")
    return m.Plan.model_validate(data["plan"])


def _check_every_step_compiled(plan: m.Plan, compiled: Compiled) -> None:
    """Defensive: every requested step must appear in the compiled trace, in order."""
    by_name = {a.name: a for a in compiled.assets}
    for table in plan.clean:
        asset = by_name.get(f"clean.{table.table}")
        ops = [t["op"] for t in (asset.trace if asset else [])][1:]
        if ops != [s.op for s in table.steps]:
            raise AssertionError(f"clean.{table.table}: compiled steps {ops} differ from the requested "
                                 f"{[s.op for s in table.steps]}")


def _pipelines_root(settings: Settings, user_id: str) -> Path:
    root = user_root(settings.workspace_root, user_id) / "pipelines"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text, encoding="utf-8")


def _asset_path(asset_name: str) -> str:
    layer, _, name = asset_name.partition(".")
    return f"pipeline/assets/{layer}/{name}.sql"


def _bruin_json(settings: Settings, root: Path, args: list[str]) -> object:
    result = run_bruin(settings, [*args, "--config-file", str(root / ".bruin.yml")],
                       cwd=root, timeout=settings.tool_timeout_s)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise PipelineError(f"bruin {args[0]} returned no JSON (exit {result.returncode}): "
                            f"{(result.stderr or result.stdout).strip()[-400:]}") from None


def _render(settings: Settings, root: Path, rel: str) -> str:
    result = run_bruin(settings, ["render", "--config-file", str(root / ".bruin.yml"), str(root / rel)],
                       cwd=root, timeout=settings.tool_timeout_s)
    if result.returncode != 0:
        raise PipelineError(f"bruin render failed for {rel}: {(result.stderr or result.stdout).strip()[-400:]}")
    return result.stdout.strip()


def _bruin_config(database: Path) -> str:
    return ("default_environment: default\nenvironments:\n  default:\n    connections:\n"
            "      duckdb:\n        - name: duckdb-default\n"
            f"          path: {json.dumps(str(database))}\n")


def build(settings: Settings, user_id: str, plan: m.Plan) -> dict:
    """Validate, write a Bruin pipeline atomically, and run `bruin validate` on it."""
    paths, schemas = resolve_sources(settings, user_id, plan)
    compiled: Compiled = compile_plan(plan, paths, schemas)
    _check_every_step_compiled(plan, compiled)

    pipeline_id = "pl_" + secrets.token_hex(8)
    root_dir = _pipelines_root(settings, user_id)
    staging = root_dir / f".staging-{pipeline_id}"
    staging.mkdir(mode=0o700)
    try:
        subprocess.run(["git", "init", "-q", str(staging)], check=True, capture_output=True, timeout=30)
        # validated against a database inside staging; re-pointed at the final folder before the rename
        _write(staging / ".bruin.yml", _bruin_config(staging / "warehouse.duckdb"))
        _write(staging / "pipeline" / "pipeline.yml",
               f"name: {plan.name}\ndefault_connections:\n  duckdb: duckdb-default\n")
        for asset in compiled.assets:
            _write(staging / _asset_path(asset.name), asset.text())

        report = _bruin_json(settings, staging, ["validate", "-o", "json", str(staging / "pipeline")])
        issues = {}
        for entry in report if isinstance(report, list) else [report]:
            for rule, found in (entry.get("issues") or {}).items():
                if found:
                    issues[rule] = found
        if issues:
            raise PipelineError(f"bruin validate rejected the generated pipeline: {json.dumps(issues)[:1500]}")

        rendered = {a.name: _render(settings, staging, _asset_path(a.name)) for a in compiled.assets}
        summary = compiled.summary()
        for name, text in rendered.items():
            summary["assets"][name]["rendered"] = text
            summary["assets"][name]["file"] = _asset_path(name)
        record = {"pipeline_id": pipeline_id, "plan": plan.name, "created_at": int(time.time()),
                  "bruin_validate": "passed", "steps_received": steps_received(plan),
                  # what a run will execute, by hash: runs refuse a pipeline whose files changed
                  "datasets": {src.table: src.dataset_id for src in plan.sources},
                  "files": {a.name: {"path": _asset_path(a.name),
                                     "sha256": hashlib.sha256(a.text().encode()).hexdigest()}
                            for a in compiled.assets},
                  **summary}
        _write(staging / "plan.json", json.dumps(plan.model_dump(mode="json"), indent=2) + "\n")
        _write(staging / "compiled.json", json.dumps(record, indent=2) + "\n")
        # bruin leaves logs/ and edits .gitignore in its working folder; neither is part of the pipeline
        shutil.rmtree(staging / "logs", ignore_errors=True)
        for leftover in staging.glob("warehouse.duckdb*"):
            leftover.unlink()
        _write(staging / ".bruin.yml", _bruin_config(root_dir / pipeline_id / "warehouse.duckdb"))
        record["config_sha256"] = hashlib.sha256((staging / ".bruin.yml").read_bytes()).hexdigest()
        record["pipeline_yml_sha256"] = hashlib.sha256((staging / "pipeline/pipeline.yml").read_bytes()).hexdigest()
        _write(staging / "compiled.json", json.dumps(record, indent=2) + "\n")
        os.rename(staging, root_dir / pipeline_id)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return record


def get(settings: Settings, user_id: str, pipeline_id: str) -> dict:
    if isinstance(pipeline_id, str) and _DRAFT_ID.fullmatch(pipeline_id):
        # live runs passed the draft straight to run_pipeline; say what to do instead
        raise PipelineError(f"{pipeline_id!r} is a draft_id, not a pipeline_id — call "
                            f"compile_plan(draft_id={pipeline_id!r}) first; it returns the pipeline_id")
    if not isinstance(pipeline_id, str) or not _PIPELINE_ID.fullmatch(pipeline_id):
        raise PipelineError(f"no pipeline {pipeline_id!r}")
    _pipelines_root(settings, user_id)
    path = resolve_user_path(settings.workspace_root, user_id, f"pipelines/{pipeline_id}")
    record = path / "compiled.json"
    if not record.is_file():
        raise PipelineError(f"no pipeline {pipeline_id!r}")
    return json.loads(record.read_text(encoding="utf-8"))


def list_all(settings: Settings, user_id: str) -> list[dict]:
    root = _pipelines_root(settings, user_id)
    out = []
    for entry in sorted(root.iterdir()):
        if _PIPELINE_ID.fullmatch(entry.name) and (entry / "compiled.json").is_file():
            rec = json.loads((entry / "compiled.json").read_text(encoding="utf-8"))
            out.append({"pipeline_id": rec["pipeline_id"], "plan": rec["plan"],
                        "created_at": rec["created_at"], "assets": sorted(rec["assets"])})
    return out


def delete(settings: Settings, user_id: str, pipeline_id: str) -> None:
    get(settings, user_id, pipeline_id)
    shutil.rmtree(resolve_user_path(settings.workspace_root, user_id, f"pipelines/{pipeline_id}"))


def clean_staging(settings: Settings) -> None:
    """Startup: remove half-built pipelines left by a crash."""
    users = Path(settings.workspace_root) / "users"
    if users.is_dir():
        for staging in users.glob("*/pipelines/.staging-*"):
            shutil.rmtree(staging, ignore_errors=True)
