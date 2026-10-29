# Security model and limits

Status: Phase 6, development only. Bound to 127.0.0.1:18443. Not deployed and
not reachable from other machines.

## What the service enforces

| Control | How | Verified by |
|---|---|---|
| Authentication | Bearer token on every HTTP request, checked by the MCP library's middleware through `TokenStoreVerifier`; anything else gets HTTP 401 before MCP handling | `test_e2e_mcp.py` (no, garbage, forged, expired, revoked tokens) |
| Token storage | `bmcp_<id>_<secret>`, 256-bit secret; only SHA-256 stored, file must be 0600 and owned by the service user | `test_auth.py`, `test_startup.py` |
| Expiry / revocation | 1–90 days; `admin revoke`; the file is re-read when it changes, so a revoked token fails on its next request, including inside an open session | `test_revocation_mid_session` |
| Caller identity | Taken from the verified token of the HTTP request that carries the tool call, never from tool arguments | `test_caller_identity_comes_from_token`, `test_identity_arguments_are_ignored` |
| Session binding | The MCP library serves a session only to the token that created it (404 otherwise) | `test_session_cannot_be_used_with_another_token` |
| Per-tool authorization | Each token lists its tools; every tool re-checks the list against the current token file | `test_per_tool_authorization` |
| Workspace isolation (application level) | Per-user folder `users/<id>/`; server-generated dataset ids; every path resolved with realpath and confined; another user's dataset reports "no dataset", the same as a missing one | `test_datasets.py`, `test_upload_and_inspect_messy_dataset_and_isolation` |
| Bruin/DuckDB confinement | Server-built SQL only. Each query: autoinstall/autoload off, `allowed_directories=[<that dataset's folder>]`, `enable_external_access=false`, `lock_configuration=true` | `test_bruin_query.py` (other user's folder, `..`, URLs, /etc/passwd, re-enable attempts, INSTALL) |
| Dataset immutability | Dataset folder 0500, files 0400, because `allowed_directories` alone still permits writes inside the folder | `test_write_inside_dataset_blocked_by_read_only_folder` |
| Pinned Bruin | SHA-256 of the binary checked against VERIFIED.txt at startup and before every run | `test_bruin_cli.py`, `test_startup.py` |
| Transport | DNS-rebinding protection: Host must equal BMCP_PUBLIC_URL's host (421), Origin must match (403); dev refuses non-loopback bind/public URL; prod requires https public URL | `test_e2e_mcp.py`, `test_config.py` |
| Limits | 100 MB upload; request body ≈ upload×4/3 + 1 MB (413); 5 GB per-user quota; per-user and total concurrency (extra calls refused, not queued); per-call timeout | `test_e2e_mcp.py`, `test_datasets.py` |
| Audit | JSON lines: every tool call (user, token id, tool, outcome, duration) and every rejected token (reason, token id); secrets redacted | `test_audit_log_has_no_secrets` |

## Plan engine (Phase 5)

| Control | How | Verified by |
|---|---|---|
| No invented columns | Each step is checked against the previous step's output schema, starting from the file's real columns (DESCRIBE); expressions and raw SQL are resolved with sqlglot `qualify` against that schema | `test_plan_engine.py`, `test_plan_bruin.py` |
| No file or system access from plan SQL | Raw SQL: one SELECT; tables only from the listed upstream assets; table functions (`read_csv`, `read_parquet`, …), `getenv`, `glob`, `read_text`, `current_setting`, `now`, `random` and unknown functions refused; no DDL/DML/COPY/ATTACH | `test_bad_queries`, e2e `unsafe` plan |
| Values cannot break out of SQL | Literals escaped, identifiers quoted; the compiler emits every SQL string itself | `test_values_are_escaped_not_spliced` |
| Atomic pipelines | Built in a staging folder; `bruin validate` (any issue fails) and `bruin render` must pass before the rename | `test_build_writes_one_validated_cleaning_asset_and_nothing_runs`, `test_invented_columns_fail_before_anything_is_written` |
| Per-user pipelines | `users/<id>/pipelines/<pl_…>`; another user's pipeline or dataset looks missing | `test_another_users_dataset_cannot_be_a_source`, e2e isolation test |

## Pipeline runs and exports (Phase 6)

| Control | How | Verified by |
|---|---|---|
| What runs is what was validated | compile records the SHA-256 of `.bruin.yml`, `pipeline.yml` and every asset; a run re-checks them and refuses changed, missing or extra files before Bruin starts | `test_tampered_pipelines_are_refused_before_bruin_runs` |
| OS-level confinement of Bruin | every `bruin run` goes through `sandbox.py` (Linux **Landlock**, unprivileged): read-only system libraries, the pinned binary and its HOME, and **only the dataset folders this pipeline uses**; write access to **only this pipeline's folder**; **no TCP**. If Landlock is unavailable the launcher refuses to run (exit 97), and the service refuses to start | `test_the_sandbox_denies_other_users_data_and_service_secrets`: planted assets reading another user's data and a secret next to the token file got `Permission denied` |
| One run at a time | file lock per pipeline; at most `BMCP_MAX_RUNS` runs at once; `BMCP_RUN_TIMEOUT_S` wall clock (killed on expiry) | `test_a_second_run…`, `test_a_run_over_its_time_limit_is_stopped` |
| Exports never let DuckDB write files | tables are read with the Python duckdb package (read-only, `enable_external_access=false`) and written row by row by the service; capped at `BMCP_MAX_EXPORT_MB` and the user's quota; staged and renamed | `test_export_size_cap`, `test_csv_export…` |
| Verified downloads | `download_export` returns chunks plus the file's SHA-256; the Swarn client reassembles, checks size and hash, and saves only on a match | agent2 `test_download_convention_saves_verified_file_in_the_workspace` |

Why Landlock: on this VM (Ubuntu 24.04, kernel 6.8) AppArmor blocks unprivileged user namespaces
(`apparmor_restrict_unprivileged_userns=1`), and no bwrap, firejail or nsjail is installed, so a
namespace sandbox would need root changes. Landlock ABI 4 is available to an ordinary user. DuckDB
needs read access to `/sys/fs/cgroup` at start-up (it reads its memory limit there), which was
found by testing.

## What it does NOT protect against

**Workspace isolation is application-level only. It is not an OS-level
security boundary.** All users' data is stored under one Linux account (the
service user) and is kept apart only by this service's code.

- **Docker group = root.** The VM users `sargam`, `sumit` and `spoo` are in the
  `docker` group. Anyone in that group can start a container that mounts `/`
  and read or change any file, including every user's datasets, the token
  file and the Bruin binary. Nothing in this service can prevent that. Real
  OS-level separation would need those accounts removed from the docker group,
  or the service moved to a host or VM they cannot administer.
- **root** can read everything.
- **Anyone who can log in as the service user** (`sargam`) can read every
  workspace and the token file.
- **A compromise of the service process** exposes all users' data, because one
  process serves everyone. Bruin runs with a minimal environment and confined
  DuckDB settings, but in the same account.
- **Tool discovery is not filtered per token.** `tools/list` shows every tool;
  authorization is enforced when a tool is called.
- **No TLS in development.** Traffic on 127.0.0.1 is plain HTTP. Remote access
  needs TLS, and that is a separate, unapproved phase.
- **Thread timeouts.** On timeout the caller gets an error and the Bruin
  subprocess is killed by its own timeout, but CPU-bound Python work (for
  example converting a large .xlsx) runs to completion in its thread.
- **Session ids** are held in memory. A restart ends all sessions; clients reconnect.

## OAuth metadata

The MCP library publishes `/.well-known/oauth-protected-resource/mcp`, which names
BMCP_PUBLIC_URL as the authorization server. This service issues tokens out of
band (`admin issue`) and has no OAuth endpoints. Clients must send a static
bearer token and must not try an OAuth flow.
