"""
Running a compiled pipeline: integrity gate → sandboxed `bruin run` → results.

A run is started in the background and polled (start → run_id; get → status),
because a run can outlast a client's call timeout.

Before Bruin starts, the INTEGRITY GATE re-reads every file Bruin will read
(.bruin.yml, pipeline.yml, each asset) and compares its SHA-256 with what
compile_plan recorded; any extra file under pipeline/ is refused too. So what
runs is exactly what was validated, not whatever is on disk now.

Bruin then runs inside the Landlock launcher (sandbox.py): it can read the
pinned binary, its HOME, system libraries and ONLY the dataset folders this
pipeline uses (read-only), write ONLY this pipeline's folder, and open no TCP
connections. Verified 2026-10-23: a planted asset reading the service's token
file or another user's dataset failed with "Permission denied".

After the run, row counts and columns are read from the warehouse with the
Python duckdb package (same DuckDB 1.5.2 as Bruin) opened read-only with
external access disabled.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import datasets, sandbox
from ..bruin_cli import bruin_env, verify_pinned_binary
from ..config import Settings
from ..paths import resolve_user_path
from . import pipeline as plans

_RUN_ID = re.compile(r"run_[0-9a-f]{16}")
_SLOTS: dict[int, threading.BoundedSemaphore] = {}
_SLOTS_LOCK = threading.Lock()
LOG_TAIL = 6000


class RunError(ValueError):
    """The run was refused or is unknown. The message is safe to show the caller."""


def _slots(settings: Settings) -> threading.BoundedSemaphore:
    with _SLOTS_LOCK:
        return _SLOTS.setdefault(settings.max_runs, threading.BoundedSemaphore(settings.max_runs))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pipeline_dir(settings: Settings, user_id: str, pipeline_id: str) -> Path:
    plans.get(settings, user_id, pipeline_id)          # validates the id and that it exists
    return resolve_user_path(settings.workspace_root, user_id, f"pipelines/{pipeline_id}")


def integrity_problems(root: Path, record: dict) -> list[str]:
    """Differences between the pipeline on disk and what compile recorded."""
    if "files" not in record or "config_sha256" not in record:
        return ["this pipeline was compiled before runs were supported; compile the plan again"]
    problems = []
    expected = {info["path"]: info["sha256"] for info in record["files"].values()}
    expected["pipeline/pipeline.yml"] = record.get("pipeline_yml_sha256", "")
    for rel, digest in expected.items():
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel} is missing")
        elif _sha(path) != digest:
            problems.append(f"{rel} changed after it was compiled")
    found = {str(p.relative_to(root)) for p in (root / "pipeline").rglob("*") if p.is_file()}
    for extra in sorted(found - set(expected)):
        problems.append(f"{extra} was not produced by compile_plan")
    if _sha(root / ".bruin.yml") != record["config_sha256"]:
        problems.append(".bruin.yml changed after it was compiled")
    return problems


def _runs_dir(root: Path) -> Path:
    path = root / "runs"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _save(root: Path, run: dict) -> None:
    path = _runs_dir(root) / f"{run['run_id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


_LINE = re.compile(r"^(PASS|FAIL|SKIP)\s+(\S+)")


def parse_output(text: str) -> dict:
    """Per-asset outcome and failure messages from `bruin run --no-color --no-timestamp`."""
    assets, errors = {}, []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if m:
            assets[m.group(2)] = m.group(1).lower()
        stripped = line.strip()
        if stripped.startswith("└──") or "Check failed" in stripped or "check failed" in stripped:
            errors.append(stripped.lstrip("└─ ").strip())
    checks = {}
    for m in re.finditer(r"Finished: (\S+?):(\S+?):(\S+)", text):
        checks[f"{m.group(1)}:{m.group(2)}:{m.group(3)}"] = "passed"
    for m in re.finditer(r"Failed: (\S+?):(\S+?):(\S+)", text):
        checks[f"{m.group(1)}:{m.group(2)}:{m.group(3)}"] = "failed"
    return {"assets": assets, "checks": checks, "errors": errors[:20]}


def table_stats(root: Path, asset_names: list[str]) -> dict:
    """Rows and columns of each built table, read without file access."""
    import duckdb
    db = root / "warehouse.duckdb"
    if not db.exists():
        return {}
    out = {}
    con = duckdb.connect(str(db), read_only=True, config={"enable_external_access": False})
    try:
        for name in asset_names:
            schema, _, table = name.partition(".")
            try:
                rows = con.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()[0]
                cols = con.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                    [schema, table]).fetchall()
                out[name] = {"rows": rows, "columns": [{"name": c, "type": t} for c, t in cols]}
            except duckdb.Error:
                out[name] = {"rows": None, "columns": [], "note": "not built"}
    finally:
        con.close()
    return out


def _scrub(text: str, settings: Settings) -> str:
    """Server paths are not the client's business; show them relative to the workspace."""
    return text.replace(str(settings.workspace_root), "<workspace>")


def start(settings: Settings, user_id: str, pipeline_id: str) -> dict:
    root = _pipeline_dir(settings, user_id, pipeline_id)
    record = json.loads((root / "compiled.json").read_text(encoding="utf-8"))
    problems = integrity_problems(root, record)
    if problems:
        raise RunError("pipeline refused — " + "; ".join(problems))
    dataset_dirs = []
    for table, dataset_id in record.get("datasets", {}).items():
        try:
            dataset_dirs.append(datasets.get(settings, user_id, dataset_id).dir)
        except datasets.DatasetError:
            raise RunError(f"source {table!r}: dataset {dataset_id} no longer exists") from None

    lock_fh = open(root / ".run.lock", "a")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        raise RunError("this pipeline is already running; poll get_run") from None
    slots = _slots(settings)
    if not slots.acquire(blocking=False):
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        raise RunError(f"the service is running {settings.max_runs} pipeline(s) already; try again shortly")

    run = {"run_id": "run_" + secrets.token_hex(8), "pipeline_id": pipeline_id, "status": "running",
           "started_at": int(time.time()), "finished_at": None}
    _save(root, run)

    def work():
        try:
            verify_pinned_binary(settings)
            cmd = sandbox.command(
                sys.executable,
                [str(settings.bruin_bin), "run", "--config-file", str(root / ".bruin.yml"),
                 "--no-color", "--no-timestamp", str(root / "pipeline")],
                ro=[settings.bruin_bin.parent, settings.bruin_home, *dataset_dirs], rw=[root])
            try:
                proc = subprocess.run(cmd, cwd=root, env=bruin_env(settings), capture_output=True,
                                      text=True, timeout=settings.run_timeout_s)
                output, code = proc.stdout + proc.stderr, proc.returncode
                status = "succeeded" if code == 0 else "failed"
                if code == sandbox.EXIT_NO_LANDLOCK:
                    status = "refused"
            except subprocess.TimeoutExpired as exc:
                output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
                code, status = None, "timeout"
            parsed = parse_output(output)
            run.update(status=status, exit_code=code, finished_at=int(time.time()),
                       assets=parsed["assets"], checks=parsed["checks"],
                       errors=[_scrub(e, settings) for e in parsed["errors"]],
                       log_tail=_scrub(output[-LOG_TAIL:], settings),
                       tables=table_stats(root, sorted(record["assets"])) if status in ("succeeded", "failed") else {})
            if status == "timeout":
                run["errors"] = [f"the run exceeded {settings.run_timeout_s}s and was stopped"]
        except Exception as exc:  # noqa: BLE001 — a run must always end with a recorded status
            run.update(status="error", finished_at=int(time.time()), errors=[f"{type(exc).__name__}: {exc}"])
        finally:
            _save(root, run)
            slots.release()
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()

    threading.Thread(target=work, name=f"bruin-run-{run['run_id']}", daemon=True).start()
    return {"run_id": run["run_id"], "pipeline_id": pipeline_id, "status": "running",
            "next": f"call get_run(pipeline_id, run_id): it waits up to {RUN_WAIT_S}s for the run to "
                    f"finish, so call it again only while status is still 'running'"}


# get_run waits this long for a running run before answering. Live runs polled
# back-to-back, tripped the client's repeated-call guard after ~3 s and gave up
# on a run that succeeded at 20 s. Kept well under Swarn's 55 s call timeout.
RUN_WAIT_S = 30


def get(settings: Settings, user_id: str, pipeline_id: str, run_id: str,
        wait_s: float = RUN_WAIT_S) -> dict:
    root = _pipeline_dir(settings, user_id, pipeline_id)
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise RunError(f"no run {run_id!r}")
    path = root / "runs" / f"{run_id}.json"
    if not path.is_file():
        raise RunError(f"no run {run_id!r} for pipeline {pipeline_id}")
    deadline = time.monotonic() + wait_s
    run = json.loads(path.read_text(encoding="utf-8"))
    while run.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(0.5)
        run = json.loads(path.read_text(encoding="utf-8"))      # _save replaces it atomically
    if run.get("status") == "running":
        run["next"] = (f"still running after waiting {wait_s:.0f}s (started "
                       f"{int(time.time()) - run['started_at']}s ago); call get_run again — large data "
                       f"can take minutes. Do not switch to another tool while it runs.")
    return run


def latest_success(settings: Settings, user_id: str, pipeline_id: str) -> dict | None:
    root = _pipeline_dir(settings, user_id, pipeline_id)
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "runs").glob("run_*.json")] \
        if (root / "runs").is_dir() else []
    done = [r for r in runs if r.get("status") == "succeeded"]
    return max(done, key=lambda r: r["finished_at"]) if done else None


def mark_interrupted(settings: Settings) -> None:
    """Startup: a run still 'running' was killed with the previous process."""
    users = Path(settings.workspace_root) / "users"
    if not users.is_dir():
        return
    for path in users.glob("*/pipelines/pl_*/runs/run_*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if run.get("status") == "running":
            run.update(status="interrupted", finished_at=int(time.time()),
                       errors=["the service stopped while this run was in progress"])
            path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
