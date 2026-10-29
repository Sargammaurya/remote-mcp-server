"""
The Bruin MCP service: MCP over streamable HTTP with bearer-token auth.

    set -a; . ./.env; set +a
    .venv/bin/python -m bruin_mcp_service.server

Request path:
  HTTP request -> body-size limit -> MCP library bearer middleware
  (TokenStoreVerifier: unknown/revoked/expired -> 401) -> MCP session
  (bound by the library to the token that created it) -> tool wrapper:
  identity re-read from THIS request's token, tool allow-list, per-user and
  global concurrency limits, timeout, audit record -> tool body in a thread.

Startup refuses to serve if settings are invalid, the Bruin binary fails its
pin check, or the token file is missing or readable by group/others.
"""

from __future__ import annotations

import asyncio
import functools
import inspect as pyinspect
import logging
import os
import shutil
import time
import typing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import anyio
from pydantic import ValidationError
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__, datasets
from .auth import (REQUIRED_SCOPE, AuthError, Caller, TokenStore, TokenStoreVerifier,
                   caller_for_tool, check_file_mode)
from .bruin_cli import BruinPinError, verify_pinned_binary
from .bruin_query import QueryError
from .config import Settings, load_settings
from .inspect_data import inspect
from .plan import exports as plan_exports
from .plan import pipeline as plans
from .plan import runs as plan_runs
from .plan.engine import PlanError
from .plan.model import parse_plan, parse_table_plan, plan_format, step_fields
from .plan.schema import plan_tool_schema, table_plan_tool_schema
from .plan.saved import SavedPlanError, instantiate, list_saved
from .logging_setup import configure_logging
from .tools import TOOL_NAMES

log = logging.getLogger("bruin_mcp_service.server")
audit = logging.getLogger("bruin_mcp_service.audit")

# Clients may show this to their model, so it states only what the tools do now.
INSTRUCTIONS = (
    "Shared Bruin service (Bruin v0.11.780, DuckDB) for data PREPARATION. Files on the client are "
    "not visible here: upload a CSV/XLSX with upload_dataset and refer to it by dataset_id. "
    "inspect_dataset reports schema and data-quality facts computed on the server, including the most "
    "common values (with counts) of low-variety text columns — a cleaning step must cover EVERY variant "
    "listed there (personal columns are masked). To prepare data, "
    "call validate_table_plan with the dataset_id and the cleaning steps (its schema lists every step "
    "and field; use validate_plan only for several tables), check that steps_received covers everything "
    "the user asked for, then compile_plan(draft_id), run_pipeline and poll get_run, then export_asset and "
    "download_export the result; export_asset returns column_summaries of the result — check what you "
    "report about it against them. Every source column starts as text; types change only through "
    "cast/parse_date. This service never engineers features or trains models — that is the client's job."
)


class _Limits:
    def __init__(self, per_user: int, total: int):
        self.per_user = per_user
        self.total = asyncio.Semaphore(total)
        self.users: dict[str, asyncio.Semaphore] = {}

    def for_user(self, user_id: str) -> asyncio.Semaphore:
        return self.users.setdefault(user_id, asyncio.Semaphore(self.per_user))


def _request_access_token(ctx: Context):
    """The verified token of the HTTP request that carried this tool call."""
    request = getattr(ctx.request_context, "request", None)
    user = request.scope.get("user") if request is not None else None
    return user.access_token if isinstance(user, AuthenticatedUser) else None


def build_server(settings: Settings, store: TokenStore) -> FastMCP:
    parts = urlsplit(settings.public_url)
    mcp = FastMCP(
        name="bruin-mcp-service",
        instructions=INSTRUCTIONS,
        host=settings.bind_host,
        port=settings.port,
        token_verifier=TokenStoreVerifier(store, resource=settings.public_url + "/mcp"),
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.public_url + "/mcp",
            required_scopes=[REQUIRED_SCOPE],
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[parts.netloc],
            allowed_origins=[settings.public_url],
        ),
    )
    limits = _Limits(settings.user_concurrency, settings.max_concurrency)

    def secured(name: str, schema: dict | None = None):
        """schema: publish this exact input schema instead of the one FastMCP derives from the
        signature. Used for the plan tools, whose arguments are validated by the plan models."""
        assert name in TOOL_NAMES

        def wrap(body: Callable[..., Any]):
            async def tool(ctx: Context, **kwargs):
                started = time.monotonic()
                entry = {"event": "tool_call", "tool": name, "user_id": None, "token_id": None}
                try:
                    access = _request_access_token(ctx)
                    if access is not None:  # for the audit record, even if refused below
                        entry.update(user_id=access.subject, token_id=access.client_id)
                    caller = caller_for_tool(store, access, name)
                    entry.update(user_id=caller.user_id, token_id=caller.token_id)
                    user_sem = limits.for_user(caller.user_id)
                    if user_sem.locked():
                        raise ToolError(f"busy: at most {settings.user_concurrency} concurrent calls per user")
                    if limits.total.locked():
                        raise ToolError("service busy: try again shortly")
                    async with user_sem, limits.total:
                        with anyio.fail_after(settings.tool_timeout_s + 10):
                            result = await anyio.to_thread.run_sync(
                                functools.partial(body, caller, **kwargs))
                    entry["outcome"] = "ok"
                    return result
                except TimeoutError:
                    entry["outcome"] = "timeout"
                    raise ToolError("tool call timed out") from None
                except AuthError as exc:
                    entry.update(outcome="denied", error=str(exc))
                    raise ToolError(f"unauthorized: {exc}") from None
                except PlanError as exc:
                    entry.update(outcome="error", error=f"{len(exc.problems)} plan problem(s)")
                    raise ToolError("the plan does not fit the data:\n" +
                                    "\n".join(f"  - {p}" for p in exc.problems)) from None
                except ValidationError as exc:
                    entry.update(outcome="error", error="plan format")
                    raise ToolError("the plan is not in the expected format:\n" + _format_errors(exc)) from None
                except (datasets.DatasetError, QueryError, plans.PipelineError, SavedPlanError,
                        plan_runs.RunError, plan_exports.ExportError, ValueError) as exc:
                    entry.update(outcome="error", error=str(exc)[:300])
                    raise ToolError(str(exc)) from None
                except ToolError as exc:
                    entry.update(outcome="refused", error=str(exc))
                    raise
                except Exception:
                    entry["outcome"] = "internal_error"
                    log.exception("tool %s failed", name)
                    raise ToolError("internal error (logged on the server)") from None
                finally:
                    entry["duration_ms"] = round((time.monotonic() - started) * 1000)
                    audit.info("tool_call", extra=entry)

            # MCP derives the input schema from the signature: Context plus the
            # body's arguments after `caller` (which never comes from the client)
            # (annotations resolved first: this module uses postponed annotations)
            hints = typing.get_type_hints(body)
            sig = pyinspect.signature(body)
            params = [p.replace(kind=pyinspect.Parameter.KEYWORD_ONLY, annotation=hints[p.name])
                      for p in list(sig.parameters.values())[1:]]
            tool.__name__ = tool.__qualname__ = name
            tool.__signature__ = pyinspect.Signature(
                [pyinspect.Parameter("ctx", pyinspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context),
                 *params],
                return_annotation=hints["return"])
            tool.__annotations__ = {"ctx": Context, **{p.name: p.annotation for p in params},
                                    "return": hints["return"]}
            mcp.tool(name=name, description=(body.__doc__ or "").strip())(tool)
            if schema is not None:
                mcp._tool_manager._tools[name].parameters = schema
            return body
        return wrap

    @secured("health")
    def health(caller: Caller) -> dict[str, Any]:
        """Service status, the pinned Bruin version, and who you are authenticated as."""
        try:
            record = verify_pinned_binary(settings)
            bruin = {"version": record["version"], "pin_ok": True}
        except BruinPinError as exc:
            log.error("bruin pin check failed: %s", exc)
            bruin = {"version": settings.bruin_expected_version, "pin_ok": False}
        token = store.record(caller.token_id)
        return {
            "status": "ok" if bruin["pin_ok"] else "degraded",
            "service_version": __version__,
            "bruin": bruin,
            "user_id": caller.user_id,
            "token_id": caller.token_id,
            "allowed_tools": sorted(caller.tools),
            "token_expires_at": token.expires_at if token else None,
        }

    @secured("upload_dataset")
    def upload_dataset(caller: Caller, filename: str, content_base64: str, sheet: str | None = None) -> dict[str, Any]:
        """Upload a CSV or XLSX file (base64) into your workspace; returns its dataset_id.

        filename is a label, but its extension (.csv or .xlsx) sets the file type.
        For .xlsx the first sheet is used unless sheet is given; it is converted
        to CSV on the server."""
        return datasets.upload(settings, caller.user_id, filename, content_base64, sheet)

    @secured("list_datasets")
    def list_datasets(caller: Caller) -> dict[str, Any]:
        """List the datasets in your workspace."""
        return {"datasets": datasets.list_all(settings, caller.user_id)}

    @secured("inspect_dataset")
    def inspect_dataset(caller: Caller, dataset_id: str, sample_rows: int = 5) -> dict[str, Any]:
        """Schema and data-quality facts for one of your datasets, computed with Bruin's DuckDB:
        column types, row count, per-column null/distinct counts, exact-duplicate rows, sample rows."""
        return inspect(settings, datasets.get(settings, caller.user_id, dataset_id), sample_rows)

    @secured("validate_table_plan", schema=table_plan_tool_schema())
    def validate_table_plan(caller: Caller, dataset_id: str, steps: list | None = None, table: str = "data",
                            name: str | None = None, description: str = "", checks: list | None = None,
                            expect_columns: list | None = None, outputs: list | None = None,
                            exports: list | None = None) -> dict[str, Any]:
        """Prepare ONE uploaded dataset: give its cleaning steps (in order), checks, and optional outputs
        as arguments. Checked against the dataset's REAL columns; nothing is built. Returns a draft_id,
        steps_received (one line per step — check it has everything the user asked for), the columns
        after every step and the SQL, or every problem found. Then call compile_plan(draft_id).
        Every source column starts as VARCHAR; change types with cast / parse_date."""
        args = {"dataset_id": dataset_id, "table": table, "description": description,
                "steps": steps or [], "checks": checks or [], "outputs": outputs or [], "exports": exports or []}
        if name is not None:
            args["name"] = name
        if expect_columns is not None:
            args["expect_columns"] = expect_columns
        return plans.validate(settings, caller.user_id, parse_table_plan(args))

    @secured("validate_plan", schema=plan_tool_schema())
    def validate_plan(caller: Caller, plan: dict[str, Any] | str) -> dict[str, Any]:
        """Prepare SEVERAL tables in one plan (for one table, validate_table_plan is simpler). Checked
        against the REAL columns of your datasets; nothing is built. Returns a draft_id, steps_received,
        per-step columns and SQL, or every problem found. Then call compile_plan(draft_id)."""
        return plans.validate(settings, caller.user_id, parse_plan(plan))

    @secured("compile_plan")
    def compile_plan(caller: Caller, draft_id: str) -> dict[str, Any]:
        """Build the plan validated as draft_id into a Bruin pipeline (one cleaning asset per table, a
        chain of CTEs), checked by `bruin validate`. Re-checks it against the data first. Returns
        pipeline_id, steps_received and every asset's SQL as Bruin will run it. Nothing is executed."""
        return plans.build(settings, caller.user_id, plans.load_draft(settings, caller.user_id, draft_id))

    @secured("list_pipelines")
    def list_pipelines(caller: Caller) -> dict[str, Any]:
        """List the pipelines you have compiled."""
        return {"pipelines": plans.list_all(settings, caller.user_id)}

    @secured("get_pipeline")
    def get_pipeline(caller: Caller, pipeline_id: str) -> dict[str, Any]:
        """A compiled pipeline: its assets, their SQL, per-step schemas and warnings."""
        return plans.get(settings, caller.user_id, pipeline_id)

    @secured("delete_pipeline")
    def delete_pipeline(caller: Caller, pipeline_id: str) -> dict[str, Any]:
        """Delete one of your compiled pipelines."""
        plans.delete(settings, caller.user_id, pipeline_id)
        return {"deleted": pipeline_id}

    @secured("get_plan_format")
    def get_plan_format(caller: Caller) -> dict[str, Any]:
        """The exact plan format: every step op and its fields, conditions, checks, outputs, types,
        and a complete valid example. Read this before writing a plan."""
        return plan_format()

    @secured("list_saved_plans")
    def list_saved_plans(caller: Caller) -> dict[str, Any]:
        """Saved plan templates (e.g. invoice_cleanup, ml_split) and the parameters each needs."""
        return {"saved_plans": list_saved()}

    @secured("instantiate_saved_plan")
    def instantiate_saved_plan(caller: Caller, name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Fill a saved plan template with your column names; returns an ordinary plan to pass to
        validate_plan / compile_plan (it gets no special treatment)."""
        return {"plan": instantiate(name, parameters)}

    @secured("run_pipeline")
    def run_pipeline(caller: Caller, pipeline_id: str) -> dict[str, Any]:
        """Start running a compiled pipeline with Bruin, in a sandbox, in the background. Returns
        run_id at once; poll get_run until status is not 'running'. Quality checks run with it
        and a failing check fails the run. Refused if the pipeline's files changed since compiling."""
        return plan_runs.start(settings, caller.user_id, pipeline_id)

    @secured("get_run")
    def get_run(caller: Caller, pipeline_id: str, run_id: str) -> dict[str, Any]:
        """Status of a run: running / succeeded / failed / timeout / refused / interrupted, with each
        asset's PASS/FAIL, check results, errors, and the rows/columns of every table built. While
        the run is going it waits up to 30 s for it to finish before answering."""
        return plan_runs.get(settings, caller.user_id, pipeline_id, run_id)

    @secured("export_asset")
    def export_asset(caller: Caller, pipeline_id: str, asset: str, format: str = "csv") -> dict[str, Any]:
        """Write a built table (e.g. 'clean.orders') of a successfully run pipeline to a CSV or XLSX
        file on the service. Returns export_id, rows, columns, size and SHA-256; fetch it with
        download_export."""
        return plan_exports.export(settings, caller.user_id, pipeline_id, asset, format)

    @secured("list_exports")
    def list_exports(caller: Caller) -> dict[str, Any]:
        """List your exports."""
        return {"exports": plan_exports.list_all(settings, caller.user_id)}

    @secured("download_export")
    def download_export(caller: Caller, export_id: str, offset: int = 0) -> dict[str, Any]:
        """One chunk (base64, up to 4 MB) of an export starting at offset, with next_offset (null at
        the end), total_bytes and the file's sha256."""
        return plan_exports.download(settings, caller.user_id, export_id, offset)

    @secured("delete_export")
    def delete_export(caller: Caller, export_id: str) -> dict[str, Any]:
        """Delete one of your exports."""
        plan_exports.delete(settings, caller.user_id, export_id)
        return {"deleted": export_id}

    @secured("delete_dataset")
    def delete_dataset(caller: Caller, dataset_id: str) -> dict[str, Any]:
        """Delete one of your datasets."""
        datasets.delete(settings, caller.user_id, dataset_id)
        return {"deleted": dataset_id}

    return mcp


class BodyLimit:
    """Refuse request bodies over `limit` bytes (declared or streamed) with 413."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for key, value in scope.get("headers", []):
            if key == b"content-length" and value.isdigit() and int(value) > self.limit:
                return await self._reject(send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise _TooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await self._reject(send)

    @staticmethod
    async def _reject(send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":"request body too large"}'})


class _TooLarge(Exception):
    pass


def _format_errors(exc) -> str:
    lines, hinted = [], set()
    fields = step_fields()
    for err in exc.errors()[:20]:
        loc = err.get("loc", ())
        where = ".".join(str(p) for p in loc)
        lines.append(f"  - {where}: {err.get('msg')}")
        # say what the step DOES accept, not only what it does not (works for clean.N.steps.M.<op>
        # in a full plan and steps.M.<op> in the flat form)
        if "steps" in loc:
            i = loc.index("steps")
            op = loc[i + 2] if len(loc) > i + 2 else None
            if op in fields and op not in hinted:
                hinted.add(op)
                accepted = ", ".join(f"{k} ({v})" for k, v in fields[op].items())
                lines.append(f"    {op} accepts: op, {accepted}")
    lines.append("  (get_plan_format lists every field and has a complete example)")
    return "\n".join(lines)


def _startup_checks(settings: Settings) -> None:
    verify_pinned_binary(settings)
    check_file_mode(settings.token_file)
    for path in (settings.workspace_root, settings.tmp_dir):
        Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
    # leftovers from a crash: query scratch dirs and half-written uploads
    for stale in Path(settings.tmp_dir).glob("q.*"):
        shutil.rmtree(stale, ignore_errors=True)
    users = Path(settings.workspace_root) / "users"
    if users.is_dir():
        for staging in users.glob("*/datasets/.staging-*"):
            datasets._force_remove(staging)
    plans.clean_staging(settings)
    plan_runs.mark_interrupted(settings)
    from . import sandbox
    if sandbox.abi_version() < 4:
        raise RuntimeError("Linux Landlock ABI >= 4 is required to run pipelines safely")


def build_app(settings: Settings):
    store = TokenStore(settings.token_file)
    mcp = build_server(settings, store)
    app = mcp.streamable_http_app()
    # base64 inflates by 4/3; allow 1 MB for the JSON-RPC envelope
    return BodyLimit(app, settings.max_upload_bytes * 4 // 3 + 1024 * 1024), mcp


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_dir, os.environ.get("BMCP_LOG_LEVEL", "INFO"))
    # the MCP library logs session ids per request at INFO; keep only its warnings
    logging.getLogger("mcp").setLevel(logging.WARNING)
    _startup_checks(settings)
    app, _ = build_app(settings)
    log.info("starting", extra={"event": "startup", "env": settings.env,
                                "bind": f"{settings.bind_host}:{settings.port}",
                                "public_url": settings.public_url, "version": __version__})
    import uvicorn
    uvicorn.run(app, host=settings.bind_host, port=settings.port, log_level="warning",
                access_log=False, timeout_graceful_shutdown=10, lifespan="on")


if __name__ == "__main__":
    main()
