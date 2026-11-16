"""
What a run changed, per cleaned table: facts for the client to report, so it does not guess.

A live run "verified via sample comparison" that satisfaction was unchanged; every
one of its 100 values had gone from 'True'/'False' to 'true'/'false'.

Exact, from the plan's own CTE chain re-run over the run's raw table:
  * rows before and after, and which step removed how many;
  * columns renamed, dropped, added, and their new types;
  * per value step (trim, replace_values, cast, …) and column it rewrites: the
    rows whose value it changed, with examples old → new. Within such a step
    every row keeps its place, so this is per cell: the engine records each
    column's expression (trace "changed") and one grouped query counts
    (old, new) pairs over the rows just before the step. For a cast, rows
    converted, rows that became NULL (did not convert), and rows WRITTEN
    differently (e.g. 'True' → 'true') and, apart, the same number written
    another way (reformatted: '0.0' → '0', '1e3' → '1000').
Values are bounded and masked like inspect's (summaries.py).

Runs in the run's thread, on the warehouse opened read-only with the query limits;
a report that cannot be made says so and never fails the run.
"""

from __future__ import annotations

from .. import summaries as sm

SHOWN = 5                    # examples per step and column
TYPED = ("cast", "parse_date")
NUMERIC = ("INTEGER", "BIGINT", "DOUBLE")


def _same_number(old: str, new: str) -> bool:
    """'20' cast to DOUBLE is written '20.0': the same number, reported apart as reformatted."""
    try:
        return float(old) == float(new)
    except ValueError:
        return False


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= sm.MAX_LEN else text[:sm.MAX_LEN] + "…"


def _examples(column: str, pairs: list[tuple]) -> list | dict:
    if sm.is_sensitive_name(column):
        return {"masked": "the column name suggests personal data"}
    if any(sm._looks_personal(str(v)) for old, new, _ in pairs for v in (old, new) if v is not None):
        return {"masked": "values look like personal data"}
    return [{"from": _text(old), "to": _text(new), "rows": int(n)} for old, new, n in pairs[:SHOWN]]


def _chain(asset: dict) -> str:
    return asset["sql"].rsplit("\nSELECT ", 1)[0]


def _rows(con, asset: dict) -> dict:
    n = len(asset["steps"]) - 1
    counts = dict(con.execute(_chain(asset) + "\n" + " UNION ALL ".join(
        f"SELECT {i} AS i, count(*) FROM s{i}" for i in range(n + 1))).fetchall())
    removed = {f"step {i} ({asset['steps'][i]['op']})": counts[i - 1] - counts[i]
               for i in range(1, n + 1) if counts[i] != counts[i - 1]}
    return {"before": counts[0], "after": counts[n], **({"removed_by": removed} if removed else {})}


def _steps(con, asset: dict) -> list[dict]:
    parts, labels, types_at = [], {}, {}
    for t in asset["steps"]:
        types_at[t["step"]] = {c["name"]: c["type"] for c in t["columns"]}
        for col, expr in t.get("changed", {}).items():
            key = len(labels)
            labels[key] = (t["step"], t["op"], col)
            parts.append(f"SELECT {key} AS k, CAST(o AS VARCHAR) AS o, CAST(v AS VARCHAR) AS v, n FROM "
                         f"(SELECT {_q(col)} AS o, {expr} AS v, count(*) AS n FROM s{t['step'] - 1} GROUP BY ALL) "
                         f"WHERE CAST(o AS VARCHAR) IS DISTINCT FROM CAST(v AS VARCHAR)")
    if not parts:
        return []
    got: dict[int, list] = {}
    for k, old, new, n in con.execute(_chain(asset) + "\n" + " UNION ALL ".join(parts) +
                                      " ORDER BY k, n DESC, o NULLS FIRST, v NULLS FIRST").fetchall():
        got.setdefault(k, []).append((old, new, n))
    out = []
    for k, (step, op, col) in labels.items():
        pairs = got.get(k, [])
        col_types = types_at[step]
        entry = {"step": f"{step} ({op})", "column": col}
        if op in TYPED:
            failed = [(o, v, n) for o, v, n in pairs if o is not None and v is None]
            converted = [(o, v, n) for o, v, n in pairs if o is not None and v is not None]
            same = [p for p in converted if col_types.get(col) in NUMERIC and _same_number(p[0], p[1])]
            written = [p for p in converted if p not in same]
            entry["became_null"] = sum(n for *_, n in failed)
            entry["written_differently"] = sum(n for *_, n in written)
            if same:
                entry["reformatted"] = sum(n for *_, n in same)
            if failed:
                entry["not_converted"] = _examples(col, failed)
            if written:
                entry["examples"] = _examples(col, written)
            if same:
                entry["reformatted_examples"] = _examples(col, same)
        else:
            entry["rows_changed"] = sum(n for *_, n in pairs)
            if pairs:
                entry["examples"] = _examples(col, pairs)
        out.append(entry)
    return out


def describe(con, record: dict) -> dict:
    """{clean asset: what the run changed in it}."""
    out = {}
    for name, asset in sorted(record["assets"].items()):
        if asset.get("kind") != "clean" or "origins" not in asset:
            continue                      # compiled before this report existed
        try:
            raw_cols = [c["name"] for c in record["assets"][asset["depends"][0]]["columns"]]
            origins = asset["origins"]
            report = {"rows": _rows(con, asset)}
            columns = {
                "renamed": {old: new for new, old in origins.items() if old is not None and old != new},
                "dropped": [c for c in raw_cols if c not in origins.values()],
                "added": [c for c, old in origins.items() if old is None],
                "retyped": {c["name"]: f"VARCHAR → {c['type']}" for c in asset["columns"]
                            if c["type"] != "VARCHAR" and origins.get(c["name"])},
            }
            report.update({k: v for k, v in columns.items() if v})
            report["by_step"] = _steps(con, asset)
            report["how_to_read"] = ("by_step: for each step that rewrites a column, the rows whose value it "
                                     "changed, with examples (from → to). A cast reports rows that became NULL "
                                     "(did not convert), rows written differently, and rows reformatted (the same "
                                     "number written another way, e.g. '0.0' → '0'). Steps and columns not "
                                     "listed changed nothing.")
            out[name] = report
        except Exception as exc:  # noqa: BLE001 — a report must never fail the run
            out[name] = {"error": f"changes could not be computed: {type(exc).__name__}: {exc}"[:300]}
    return out
