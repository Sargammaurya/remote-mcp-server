"""
Dataset inspection through Bruin's DuckDB.

Reports what the data looks like: columns with DuckDB-detected types, row
count, per-column null and distinct counts, exact-duplicate rows, and a few
sample rows. It draws no conclusions about what preparation is needed; that
is the client's (Swarn's) decision.

Every statement is built here from the dataset's own column names (quoted as
identifiers) and runs under bruin_query's guard with allowed_directories set
to that one dataset's folder.

Distinct counts come from ONE grouping over (column, value hash) pairs, which
DuckDB can spill to disk, instead of one count(DISTINCT) per column: those
cannot spill, and 401 of them ran out of the per-query memory limit. Statements
are split into batches under MAX_STATEMENT, since each is one command-line
argument to Bruin.
"""

from __future__ import annotations

from .bruin_query import guarded_query, sql_ident, sql_string
from .config import Settings
from . import summaries
from .datasets import Dataset

MAX_SAMPLE_ROWS = 50
MAX_STATEMENT = 100_000         # characters; Linux caps a single argv string at 128 KiB


def _batches(items: list, size) -> list[list]:
    """Consecutive groups of `items` whose size(item) sums to at most MAX_STATEMENT."""
    out, current, used = [], [], 0
    for item in items:
        n = size(item)
        if current and used + n > MAX_STATEMENT:
            out.append(current)
            current, used = [], 0
        current.append(item)
        used += n
    return out + [current] if current else out


def _source(ds: Dataset) -> str:
    # sample_size=-1: type detection reads the whole file, not the first rows
    return f"read_csv({sql_string(str(ds.data_csv))}, header=true, sample_size=-1)"


def inspect(settings: Settings, ds: Dataset, sample_rows: int = 5) -> dict:
    if not isinstance(sample_rows, int) or not 0 <= sample_rows <= MAX_SAMPLE_ROWS:
        raise ValueError(f"sample_rows must be 0..{MAX_SAMPLE_ROWS}")
    src = _source(ds)

    described = guarded_query(settings, ds.dir, f"DESCRIBE SELECT * FROM {src}")
    columns = [{"name": r[0], "type": r[1]} for r in described["rows"]]

    # per column: nulls and exact distinct counts; with the first batch, the row
    # count and exact-duplicate rows. hash() keeps one value column for UNPIVOT
    # (one type, compact); NULL stays NULL so UNPIVOT drops it.
    stats: dict[int, tuple[int, int]] = {}
    fragments = [(i, f"if({sql_ident(c['name'])} IS NULL, NULL, hash({sql_ident(c['name'])})) AS \"{i}\"")
                 for i, c in enumerate(columns)]
    for n, batch in enumerate(_batches(fragments, lambda f: len(f[1]) + 2)):
        head = ("(SELECT count(*) FROM t) AS row_count, "
                "(SELECT count(*) FROM t) - (SELECT count(*) FROM (SELECT DISTINCT * FROM t)) AS duplicate_rows"
                if n == 0 else "NULL AS row_count, NULL AS duplicate_rows")
        got = guarded_query(
            settings, ds.dir,
            f"WITH t AS MATERIALIZED (SELECT * FROM {src}), "
            f"h AS (SELECT {', '.join(f for _, f in batch)} FROM t), "
            f"u AS (UNPIVOT h ON COLUMNS(*) INTO NAME c VALUE v), "
            f"g AS (SELECT CAST(c AS INTEGER) AS c, count(*) AS d, sum(k) AS non_null "
            f"FROM (SELECT c, v, count(*) AS k FROM u GROUP BY c, v) GROUP BY c) "
            f"SELECT {head}, g.c, g.d, g.non_null FROM (SELECT 1) AS one LEFT JOIN g ON true",
        )
        for row_count, duplicate_rows, c_index, distinct, non_null in got["rows"]:
            if n == 0:
                total, duplicates = row_count, duplicate_rows
            if c_index is not None:
                stats[c_index] = (int(distinct), int(non_null))
    for i, col in enumerate(columns):
        distinct, non_null = stats.get(i, (0, 0))          # a column of only NULLs has no pairs
        col["null_count"] = total - non_null
        col["distinct_count"] = distinct

    # which values the text columns really hold (bounded and masked; see summaries.py)
    listable = [(i, c["name"]) for i, c in enumerate(columns)
                if c["type"] == "VARCHAR" and c["distinct_count"] <= summaries.MAX_DISTINCT
                and not summaries.is_sensitive_name(c["name"])]
    top: dict[int, list] = {}
    for batch in _batches(listable, lambda f: 4 * len(sql_ident(f[1])) + 120):   # name ×4 per part in top_values_sql
        got = guarded_query(settings, ds.dir, summaries.top_values_sql(src, batch, sql_ident))
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
        "row_count": total,
        "duplicate_rows": duplicates,
        "columns": columns,
        "sample": sample,
        "engine": "bruin query (DuckDB), types detected by DuckDB's CSV sniffer",
    }
