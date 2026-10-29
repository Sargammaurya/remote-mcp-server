# bruin-mcp-service

One shared Bruin MCP service on the VM that remote Swarn clients call, so users
never install Bruin themselves. Separate from `agent2`.

Status: Phase 6. An MCP server over streamable HTTP with bearer-token auth, dataset
upload and inspection, a plan engine (structured plan → validated Bruin pipeline), and
sandboxed execution with CSV/XLSX export and verified chunked download. Development only,
on 127.0.0.1:18443. Not deployed, no remote access,
not production-ready. See [docs/SECURITY.md](docs/SECURITY.md) and
[docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md).

## Layout

```
src/bruin_mcp_service/
  server.py         FastMCP app, tool wrapper (identity, allow-list, limits, audit), startup checks
  auth.py           token file, TokenVerifier for the MCP library, per-tool authorization
  admin.py          init / issue / revoke / list tokens
  datasets.py       per-user uploads (CSV, XLSX→CSV), quota, read-only dataset folders
  bruin_query.py    server-built SQL through pinned Bruin's DuckDB, confined to one folder
  inspect_data.py   schema and data-quality facts for a dataset
  config.py         settings from env vars only; dev refuses non-loopback binds
  paths.py          per-user confinement under <workspace>/users/<id>/ (realpath)
  logging_setup.py  JSON-lines logs with secret redaction, never stdout
  bruin_cli.py      the only way Bruin runs: offline sha256 pin check, minimal env
  plan/model.py     the plan format (pydantic, unknown fields rejected)
  plan/engine.py    schema-flow validation + SQL compilation (one CTE chain per table)
  plan/rawsql.py    sqlglot checks for derived expressions and the raw-SQL fallback
  plan/pipeline.py  atomic pipeline writing, `bruin validate` + `bruin render`
  plan/saved.py     saved plan templates (plan/saved/*.json) — same engine
  plan/runs.py      integrity gate + sandboxed background `bruin run` + results
  plan/exports.py   built table → CSV/XLSX (read-only DuckDB) → chunked download
  sandbox.py        Landlock launcher: folder allow-list, no TCP (stdlib only)
scripts/
  fetch_bruin.sh            download + verify the pinned Bruin release into vendor/
  probe_duckdb_version.py   report the DuckDB version inside the pinned Bruin
  smoke_client.py           manual check of a running server with the MCP SDK client
deploy/bruin-mcp.service    proposed systemd unit — NOT installed
vendor/bruin/v0.11.780/     pinned binary (not committed) + VERIFIED.txt
var/                        runtime data (not committed): workspaces, tokens, logs, Bruin HOME
```

## Tools

| Tool | Does |
|---|---|
| `health` | status, Bruin pin check, your user id, token id, allowed tools, expiry |
| `upload_dataset(filename, content_base64, sheet?)` | store a CSV/XLSX in your workspace → `dataset_id` |
| `list_datasets()` | your datasets |
| `inspect_dataset(dataset_id, sample_rows=5)` | DuckDB types, row count, null/distinct per column, exact duplicates, sample rows, all computed with Bruin |
| `delete_dataset(dataset_id)` | remove one of your datasets |
| `validate_table_plan(dataset_id, steps, checks?, outputs?, …)` | ONE table, flat arguments; checked against the real columns → `draft_id`, `steps_received`, per-step columns and SQL |
| `validate_plan(plan)` | several tables in one full plan; same result |
| `compile_plan(draft_id)` | build exactly the validated draft into a Bruin pipeline (checked by Bruin) → `pipeline_id`, rendered SQL |
| `get_plan_format()` | every field, notes and a complete example |
| `list_pipelines()` / `get_pipeline(id)` / `delete_pipeline(id)` | your compiled pipelines |
| `list_saved_plans()` / `instantiate_saved_plan(name, parameters)` | plan templates (invoice_cleanup, ml_split) |
| `run_pipeline(pipeline_id)` → `get_run(pipeline_id, run_id)` | run it in the sandbox (background); per-asset PASS/FAIL, checks, rows per table |
| `export_asset(pipeline_id, asset, format)` / `list_exports` / `delete_export` | built table → CSV/XLSX on the service |
| `download_export(export_id, offset)` | one ≤4 MB base64 chunk + next_offset + file SHA-256 |

## Setup (as the service user)

```sh
cp .env.example .env            # then replace /srv/bruin-mcp-service with this checkout's path
chmod 600 .env
sh scripts/fetch_bruin.sh       # skip if vendor/bruin/v0.11.780/bruin already exists
UV_PYTHON=/usr/bin/python3.12 UV_HTTP_TIMEOUT=300 uv sync --frozen
.venv/bin/python -m pytest      # 209 tests; the e2e tests use 127.0.0.1:18443
set -a; . ./.env; set +a; .venv/bin/python -I scripts/probe_duckdb_version.py
```

## Run in development and check by hand

```sh
set -a; . ./.env; set +a
export PYTHONPATH=src
.venv/bin/python -I -m bruin_mcp_service.admin init
(umask 077; .venv/bin/python -I -m bruin_mcp_service.admin issue --user alice --all-tools > var/auth/alice.token)
(cd src && ../.venv/bin/python -I -m bruin_mcp_service.server) &      # 127.0.0.1:18443
BMCP_URL=http://127.0.0.1:18443/mcp BMCP_TOKEN_FILE_PATH=var/auth/alice.token \
  .venv/bin/python -I scripts/smoke_client.py some_synthetic.csv
.venv/bin/python -I -m bruin_mcp_service.admin list
.venv/bin/python -I -m bruin_mcp_service.admin revoke <token_id>
```

The first DuckDB use downloads Bruin's DuckDB ADBC driver from
dbc-cdn.columnar.tech into `$BRUIN_HOME/.config/adbc/`. The Python `duckdb`
package is pinned to the same version (1.5.2).
