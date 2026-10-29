"""
Report the DuckDB version built into the pinned Bruin binary.

The service's Python `duckdb` package is pinned to this version so that both
read the same database files. Runs Bruin through bruin_cli.run_bruin (pin
check, dedicated HOME, telemetry opt-out) against a throwaway DuckDB file
under var/tmp. `bruin query` refuses to run outside a git repository even
with --config-file, so the throwaway directory is made one (`git init`, no
commits). It is removed afterwards.

On first DuckDB use Bruin downloads its DuckDB ADBC driver into
$BRUIN_HOME/.config/adbc/ (from dbc-cdn.columnar.tech); later runs reuse it.

Usage (settings from the environment, see .env.example):
    python3 -I scripts/probe_duckdb_version.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bruin_mcp_service.bruin_cli import run_bruin  # noqa: E402
from bruin_mcp_service.config import load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    tmp_root = settings.tmp_dir
    tmp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe_dir = Path(tempfile.mkdtemp(prefix="duckdb-probe.", dir=tmp_root))
    try:
        subprocess.run(["git", "init", "-q", str(probe_dir)], check=True)
        config = probe_dir / ".bruin.yml"
        config.write_text(
            "default_environment: default\n"
            "environments:\n"
            "  default:\n"
            "    connections:\n"
            "      duckdb:\n"
            "        - name: duckdb-default\n"
            f"          path: {probe_dir / 'probe.duckdb'}\n",
            encoding="utf-8",
        )
        result = run_bruin(
            settings,
            ["query", "--config-file", str(config), "-c", "duckdb-default",
             "-q", "SELECT version() AS duckdb_version", "-o", "json"],
            cwd=probe_dir,
            timeout=300,
        )
        print("exit code:", result.returncode)
        print("stdout:", result.stdout.strip())
        if result.stderr.strip():
            print("stderr:", result.stderr.strip())
        if result.returncode != 0:
            return 1
        payload = json.loads(result.stdout)
        print("duckdb_version:", payload["rows"][0][0])
        return 0
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
