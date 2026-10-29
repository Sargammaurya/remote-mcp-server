"""
Read-only queries through the pinned Bruin's DuckDB, confined to one folder.

Only SQL built by this service runs here; no client SQL. Each call:
  - gets a fresh temp directory (a git repo, because `bruin query` needs one;
    Bruin also writes logs/ and .gitignore into it) with its own .bruin.yml
    and throwaway DuckDB file, removed afterwards;
  - prefixes the statement with the guard below. `bruin query -q` returns
    only the last statement's result, and returns nothing at all when the
    batch starts with SET, hence the leading SELECT. Once
    lock_configuration=true runs, no later statement can change a setting.

Verified against Bruin v0.11.780 / DuckDB v1.5.2 (2026-10-22): files outside
allowed_directories, `..` paths, http(s) URLs and INSTALL are refused.
allowed_directories still permits WRITES inside the folder (COPY TO, ATTACH),
which is why dataset folders are made read-only on disk (datasets.py).

Bruin exits 0 when a query fails and reports {"error": ...} in its JSON, so
both are checked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .bruin_cli import run_bruin
from .config import Settings


class QueryError(RuntimeError):
    """Bruin/DuckDB rejected or failed the query."""


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def guard_prefix(allowed_dir: Path) -> str:
    allowed = sql_string(str(allowed_dir).rstrip("/") + "/")
    return (
        "SELECT 1 AS bmcp_guard; "
        "SET autoinstall_known_extensions=false; "
        "SET autoload_known_extensions=false; "
        f"SET allowed_directories=[{allowed}]; "
        "SET enable_external_access=false; "
        "SET lock_configuration=true; "
    )


def guarded_query(settings: Settings, allowed_dir: Path, statement: str,
                  timeout: float | None = None) -> dict:
    """Run one server-built statement; return Bruin's JSON ({columns, rows})."""
    settings.tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix="q.", dir=settings.tmp_dir))
    try:
        subprocess.run(["git", "init", "-q", str(work)], check=True,
                       capture_output=True, timeout=30)
        config = work / ".bruin.yml"
        config.write_text(
            "default_environment: default\n"
            "environments:\n"
            "  default:\n"
            "    connections:\n"
            "      duckdb:\n"
            "        - name: scratch\n"
            f"          path: {json.dumps(str(work / 'scratch.duckdb'))}\n",
            encoding="utf-8",
        )
        try:
            result = run_bruin(
                settings,
                ["query", "--config-file", str(config), "-c", "scratch", "-o", "json",
                 "-q", guard_prefix(allowed_dir) + statement],
                cwd=work,
                timeout=timeout or settings.tool_timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise QueryError("query timed out") from None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise QueryError(f"Bruin returned no JSON (exit {result.returncode})") from None
        if result.returncode != 0 or "error" in payload:
            raise QueryError(str(payload.get("error", f"bruin exited {result.returncode}")))
        if "rows" not in payload:
            raise QueryError("Bruin returned no result rows")
        return payload
    finally:
        shutil.rmtree(work, ignore_errors=True)
