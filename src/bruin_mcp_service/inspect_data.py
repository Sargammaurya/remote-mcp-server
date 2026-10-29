"""
Dataset inspection through Bruin's DuckDB.

Reports what the data looks like: columns with DuckDB-detected types, row
count, per-column null and distinct counts, exact-duplicate rows, and a few
sample rows. It draws no conclusions about what preparation is needed; that
is the client's (Swarn's) decision.

Every statement is built here from the dataset's own column names (quoted as
identifiers) and runs under bruin_query's guard with allowed_directories set
to that one dataset's folder.
"""

from __future__ import annotations

from .bruin_query import guarded_query, sql_ident, sql_string
from .config import Settings
from . import summaries
from .datasets import Dataset

MAX_SAMPLE_ROWS = 50


def _source(ds: Dataset) -> str:
    # sample_size=-1: type detection reads the whole file, not the first rows
    return f"read_csv({sql_string(str(ds.data_csv))}, header=true, sample_size=-1)"


def inspect(settings: Settings, ds: Dataset, sample_rows: int = 5) -> dict:
    if not isinstance(sample_rows, int) or not 0 <= sample_rows <= MAX_SAMPLE_ROWS:
        raise ValueError(f"sample_rows must be 0..{MAX_SAMPLE_ROWS}")
    src = _source(ds)

    described = guarded_query(settings, ds.dir, f"DESCRIBE SELECT * FROM {src}")
    columns = [{"name": r[0], "type": r[1]} for r in described["rows"]]

    stats = ["count(*) AS row_count"]
    for i, col in enumerate(columns):
        q = sql_ident(col["name"])
        stats.append(f"count(*) - count({q}) AS n{i}")
        stats.append(f"count(DISTINCT {q}) AS d{i}")
    profile = guarded_query(
        settings, ds.dir,
        f"WITH t AS (SELECT * FROM {src}) SELECT {', '.join(stats)}, "
        f"count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM t)) AS duplicate_rows FROM t",
    )
    row = profile["rows"][0]
    names = [c["name"] for c in profile["columns"]]
    values = dict(zip(names, row))
    for i, col in enumerate(columns):
        col["null_count"] = values[f"n{i}"]
        col["distinct_count"] = values[f"d{i}"]

    # which values the text columns really hold (bounded and masked; see summaries.py)
    listable = [(i, c["name"]) for i, c in enumerate(columns)
                if c["type"] == "VARCHAR" and c["distinct_count"] <= summaries.MAX_DISTINCT
                and not summaries.is_sensitive_name(c["name"])]
    top: dict[int, list] = {}
    if listable:
        got = guarded_query(settings, ds.dir, summaries.top_values_sql(src, listable, sql_ident))
        for c_index, value, count in got["rows"]:
            top.setdefault(c_index, []).append((value, count))
    for i, col in enumerate(columns):
        if col["type"] == "VARCHAR":
            col["values"] = summaries.summarize(col["name"], col["distinct_count"], top.get(i, []))

    sample = {"columns": [c["name"] for c in columns], "rows": []}
    if sample_rows:
        got = guarded_query(settings, ds.dir, f"SELECT * FROM {src} LIMIT {sample_rows}")
        sample["rows"] = got["rows"]

    return {
        "dataset_id": ds.dataset_id,
        "filename": ds.meta["filename"],
        "kind": ds.meta["kind"],
        **({"sheet": ds.meta["sheet"]} if "sheet" in ds.meta else {}),
        "row_count": values["row_count"],
        "duplicate_rows": values["duplicate_rows"],
        "columns": columns,
        "sample": sample,
        "engine": "bruin query (DuckDB), types detected by DuckDB's CSV sniffer",
    }
