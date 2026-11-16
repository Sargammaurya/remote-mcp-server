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

The guard also caps the query's memory, threads and spill-to-disk (settings
BMCP_QUERY_*). Over its memory DuckDB spills to the call's temp directory, and
fails with "Out of Memory Error" rather than taking the machine — unguarded,
profiling a 19 MB, 3,001-column file used 25 GB.

Bruin exits 0 when a query fails and reports {"error": ...} in its JSON, so
both are checked.
"""

from __future__ import annotations

import errno
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


def guard_prefix(allowed_dir: Path, settings: Settings) -> str:
    allowed = sql_string(str(allowed_dir).rstrip("/") + "/")
    return (
        "SELECT 1 AS bmcp_guard; "
        "SET autoinstall_known_extensions=false; "
        "SET autoload_known_extensions=false; "
        f"SET allowed_directories=[{allowed}]; "
        "SET enable_external_access=false; "
        f"SET memory_limit='{settings.query_memory_mb}MB'; "
        f"SET threads={settings.query_threads}; "
        f"SET max_temp_directory_size='{settings.query_spill_gb}GB'; "
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
                 "-q", guard_prefix(allowed_dir, settings) + statement],
                cwd=work,
                timeout=timeout or settings.tool_timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise QueryError("query timed out") from None
        except OSError as exc:
            if exc.errno == errno.E2BIG:        # one argv string is capped at 128 KiB
                raise QueryError("the query is too long to run (more than 128 KB of SQL)") from None
            raise
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise QueryError(f"Bruin returned no JSON (exit {result.returncode})") from None
        if result.returncode != 0 or "error" in payload:
            message = str(payload.get("error", f"bruin exited {result.returncode}"))
            if "Out of Memory" in message:
                message = (f"the query needed more than the service's {settings.query_memory_mb} MB "
                           f"per query (BMCP_QUERY_MEMORY_MB): {message}")
            raise QueryError(message)
        if "rows" not in payload:
            raise QueryError("Bruin returned no result rows")
        return payload
    finally:
        shutil.rmtree(work, ignore_errors=True)
