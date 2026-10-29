"""
Exports: a built table → a CSV or XLSX file → downloaded by the client in chunks.

The table is read from the pipeline's warehouse with the Python duckdb package
(DuckDB 1.5.2, the version Bruin uses) opened read-only with external access
disabled, and written row by row by this service — DuckDB never writes files.
An export needs a successful run of the pipeline. Files go to
<workspace>/users/<id>/exports/<export_id>.<ext>, staged and renamed into place,
capped at BMCP_MAX_EXPORT_MB and counted against the user's quota.

download_export returns one chunk (base64) per call with the next offset and
the whole file's SHA-256, so the client can reassemble and verify it.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import decimal
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from ..config import Settings
from ..paths import resolve_user_path, user_root
from . import pipeline as plans
from . import runs

_EXPORT_ID = re.compile(r"ex_[0-9a-f]{16}")
CHUNK_BYTES = 4 * 1024 * 1024


class ExportError(ValueError):
    pass


class _TooLarge(Exception):
    pass


def _root(settings: Settings, user_id: str) -> Path:
    path = user_root(settings.workspace_root, user_id) / "exports"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    return str(value)


def export(settings: Settings, user_id: str, pipeline_id: str, asset: str, fmt: str = "csv") -> dict:
    import duckdb

    if fmt not in ("csv", "xlsx"):
        raise ExportError("format must be csv or xlsx")
    record = plans.get(settings, user_id, pipeline_id)
    if asset not in record["assets"]:
        raise ExportError(f"{asset!r} is not an asset of pipeline {pipeline_id} "
                          f"(assets: {sorted(record['assets'])})")
    if runs.latest_success(settings, user_id, pipeline_id) is None:
        raise ExportError("this pipeline has no successful run yet; run_pipeline first")
    pipeline_root = resolve_user_path(settings.workspace_root, user_id, f"pipelines/{pipeline_id}")

    from ..datasets import _dir_bytes
    remaining = settings.user_quota_bytes - _dir_bytes(user_root(settings.workspace_root, user_id))
    if remaining <= 0:
        raise ExportError(f"the {settings.user_quota_gb} GB per-user quota is used up; delete datasets, "
                          f"pipelines or exports first")
    budget = min(settings.max_export_bytes, remaining)

    export_id = "ex_" + secrets.token_hex(8)
    out_dir = _root(settings, user_id)
    final = out_dir / f"{export_id}.{fmt}"
    staging = out_dir / f".staging-{export_id}.{fmt}"
    schema, _, table = asset.partition(".")
    con = duckdb.connect(str(pipeline_root / "warehouse.duckdb"), read_only=True,
                         config={"enable_external_access": False})
    rows = 0
    try:
        cur = con.execute(f'SELECT * FROM "{schema}"."{table}"')
        columns = [d[0] for d in cur.description]
        if fmt == "csv":
            with open(staging, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(columns)
                while batch := cur.fetchmany(10000):
                    for row in batch:
                        writer.writerow([_cell(v) for v in row])
                    rows += len(batch)
                    if fh.tell() > budget:
                        raise _TooLarge()
        else:
            import openpyxl
            wb = openpyxl.Workbook(write_only=True)
            ws = wb.create_sheet(table[:31])
            ws.append(columns)
            while batch := cur.fetchmany(10000):
                for row in batch:
                    ws.append([None if v is None else (str(v) if isinstance(v, decimal.Decimal) else v)
                               for v in row])
                rows += len(batch)
                if rows > 1_048_000:
                    raise ExportError("too many rows for an XLSX sheet; export as CSV")
            wb.save(staging)
            if staging.stat().st_size > budget:
                raise _TooLarge()
        summaries = _column_summaries(con, schema, table)
    except _TooLarge:
        staging.unlink(missing_ok=True)
        raise ExportError(f"the export would exceed {budget // (1024 * 1024)} MB (export cap or remaining "
                          f"quota)") from None
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    os.chmod(staging, 0o400)
    os.rename(staging, final)
    meta = {"export_id": export_id, "pipeline_id": pipeline_id, "asset": asset, "format": fmt,
            "rows": rows, "columns": columns, "bytes": final.stat().st_size,
            "sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
            "filename": f"{table}.{fmt}", "created_at": int(time.time()),
            # bounded, masked value summaries of the exported table (see summaries.py), so a
            # client can check what it says about the result against what is in it
            "column_summaries": summaries}
    (out_dir / f"{export_id}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    # live runs stopped here, as if the export were the result; it is only on the service so far
    return {**meta, "next_step": f"The file is on the service, not with you yet: call download_export with "
                                 f"export_id={export_id!r} to fetch it before using or reporting the result."}


def _column_summaries(con, schema: str, table: str) -> dict:
    from .. import summaries as sm
    out = {}
    cols = con.execute("SELECT column_name, data_type FROM information_schema.columns "
                       "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                       [schema, table]).fetchall()
    for name, dtype in cols:
        if dtype != "VARCHAR":
            continue
        ident = '"' + name.replace('"', '""') + '"'
        nulls, distinct = con.execute(
            f'SELECT count(*) - count({ident}), count(DISTINCT {ident}) FROM "{schema}"."{table}"').fetchone()
        top = []
        if distinct <= sm.MAX_DISTINCT and not sm.is_sensitive_name(name):
            top = con.execute(f'SELECT {ident}, count(*) AS n FROM "{schema}"."{table}" WHERE {ident} IS NOT NULL '
                              f'GROUP BY 1 ORDER BY n DESC, 1 LIMIT {sm.TOP_K + 1}').fetchall()
        out[name] = {"nulls": nulls, **sm.summarize(name, distinct, top)}
    return out


def _meta(settings: Settings, user_id: str, export_id: str) -> tuple[dict, Path]:
    if not isinstance(export_id, str) or not _EXPORT_ID.fullmatch(export_id):
        raise ExportError(f"no export {export_id!r}")
    _root(settings, user_id)
    meta_path = resolve_user_path(settings.workspace_root, user_id, f"exports/{export_id}.json")
    if not meta_path.is_file():
        raise ExportError(f"no export {export_id!r}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return meta, resolve_user_path(settings.workspace_root, user_id, f"exports/{export_id}.{meta['format']}")


def download(settings: Settings, user_id: str, export_id: str, offset: int = 0) -> dict:
    meta, path = _meta(settings, user_id, export_id)
    if not isinstance(offset, int) or offset < 0 or offset > meta["bytes"]:
        raise ExportError(f"offset must be between 0 and {meta['bytes']}")
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(CHUNK_BYTES)
    nxt = offset + len(chunk)
    return {"export_id": export_id, "filename": meta["filename"], "offset": offset,
            "next_offset": nxt if nxt < meta["bytes"] else None, "total_bytes": meta["bytes"],
            "sha256": meta["sha256"], "content_base64": base64.b64encode(chunk).decode("ascii")}


def list_all(settings: Settings, user_id: str) -> list[dict]:
    root = _root(settings, user_id)
    out = []
    for path in sorted(root.glob("ex_*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        out.append({k: meta[k] for k in ("export_id", "pipeline_id", "asset", "format", "rows", "bytes",
                                          "filename", "created_at")})
    return out


def delete(settings: Settings, user_id: str, export_id: str) -> None:
    meta, path = _meta(settings, user_id, export_id)
    path.unlink(missing_ok=True)
    (path.parent / f"{export_id}.json").unlink(missing_ok=True)
