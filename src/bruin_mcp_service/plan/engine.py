"""
Schema-flow validation and SQL compilation.

For each source table the engine starts from the REAL schema of the uploaded
file (read by the caller with DESCRIBE; every column VARCHAR, because the raw
layer reads the CSV as text and types change only through explicit steps),
then walks the steps in order. Each step:

  * checks that every column it names exists in the PREVIOUS step's output,
    with the right type for the operation, and that any name it creates is
    new (case-insensitively — DuckDB identifiers are case-insensitive);
  * produces the next schema and one CTE: an explicit column list over the
    previous CTE, never SELECT *, so every reference is to a known column.

Any error stops validation for that table and is reported with the step
number; nothing is written by this module. A column a step did not produce
cannot be used later, so an invented column is caught at the step that first
mentions it.

Compiled SQL shapes, all checked against Bruin v0.11.780 / DuckDB 1.5.2 on
2026-10-23 (`bruin validate`, `bruin render`, `bruin run`):

  raw.<t>    SELECT * FROM read_csv('<server path>', header=true, all_varchar=true)
  clean.<t>  WITH s0 AS MATERIALIZED (SELECT * FROM raw.<t>), s1 AS MATERIALIZED (…), …
             SELECT <final columns> FROM sN
  mart.<n>    aggregate / split part / validated raw SQL over earlier assets

Every CTE is MATERIALIZED: without it DuckDB 1.5.2 raised an internal binder
error on chains combining TRY_CAST, string functions and a window (dedupe).
"""

from __future__ import annotations

import datetime as _dt
import difflib
from dataclasses import dataclass, field
from typing import Optional

from . import model as m
from .rawsql import SqlCheckError, check_expression, check_query, ensure_parses

NUMERIC = ("BIGINT", "INTEGER", "DOUBLE")


class PlanError(ValueError):
    """The plan does not fit the data. `problems` lists every issue found."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def q(name: str) -> str:
    """Quote an identifier for DuckDB."""
    return '"' + str(name).replace('"', '""') + '"'


def lit(value) -> str:
    """A SQL literal for a plan value (strings escaped; bools/numbers as-is)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


@dataclass
class Schema:
    """Ordered columns → type, with case-insensitive lookup.

    `case` remembers columns an earlier step upper- or lower-cased, so a later
    replace_values key that can no longer match is reported."""
    cols: dict[str, str] = field(default_factory=dict)
    case: dict[str, str] = field(default_factory=dict)

    def copy(self) -> "Schema":
        return Schema(dict(self.cols), dict(self.case))

    def find(self, name: str) -> Optional[str]:
        if name in self.cols:
            return name
        hits = [c for c in self.cols if c.lower() == str(name).lower()]
        return hits[0] if len(hits) == 1 else None

    def has_new(self, name: str, ignore: Optional[str] = None) -> bool:
        return any(c.lower() == name.lower() and c != ignore for c in self.cols)

    def as_list(self) -> list[dict]:
        return [{"name": n, "type": t} for n, t in self.cols.items()]


class _Ctx:
    """Error collection for one table/output, prefixed with where it happened."""

    def __init__(self, where: str):
        self.where, self.problems = where, []

    def fail(self, msg: str) -> None:
        self.problems.append(f"{self.where}: {msg}")


def _missing(schema: Schema, name: str) -> str:
    near = difflib.get_close_matches(name, list(schema.cols), n=2, cutoff=0.6)
    hint = f" — did you mean {', '.join(repr(n) for n in near)}?" if near else ""
    return f"column {name!r} does not exist here (available: {list(schema.cols)}){hint}"


def _resolve(schema: Schema, name: str, ctx: _Ctx, types: Optional[tuple] = None, why: str = "") -> Optional[str]:
    found = schema.find(name)
    if found is None:
        ctx.fail(_missing(schema, name))
        return None
    if types and schema.cols[found] not in types:
        ctx.fail(f"column {found!r} is {schema.cols[found]}, but {why or 'this step'} needs {'/'.join(types)}")
        return None
    return found


def _value_fits(value, sql_type: str) -> bool:
    if sql_type in NUMERIC:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and \
            (sql_type == "DOUBLE" or float(value).is_integer())
    if sql_type == "BOOLEAN":
        return isinstance(value, bool)
    if sql_type in ("DATE", "TIMESTAMP"):
        if not isinstance(value, str):
            return False
        try:
            (_dt.date.fromisoformat if sql_type == "DATE" else _dt.datetime.fromisoformat)(value)
            return True
        except ValueError:
            return False
    return isinstance(value, str)


def _typed_lit(value, sql_type: str) -> str:
    if sql_type in ("DATE", "TIMESTAMP"):
        return f"{sql_type} {lit(value)}"
    return lit(value)


# ─── conditions ────────────────────────────────────────────────────────────────

_OPS = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}


def _condition(cond, schema: Schema, ctx: _Ctx) -> Optional[str]:
    if isinstance(cond, m.AllOf) or isinstance(cond, m.AnyOf):
        parts = [_condition(c, schema, ctx) for c in (cond.all if isinstance(cond, m.AllOf) else cond.any)]
        if any(p is None for p in parts):
            return None
        joiner = " AND " if isinstance(cond, m.AllOf) else " OR "
        return "(" + joiner.join(parts) + ")"
    col = _resolve(schema, cond.column, ctx)
    if col is None:
        return None
    ctype = schema.cols[col]
    if cond.operator == "is_null":
        return f"{q(col)} IS NULL"
    if cond.operator == "not_null":
        return f"{q(col)} IS NOT NULL"
    if cond.operator == "like":
        if ctype != "VARCHAR":
            ctx.fail(f"like needs a VARCHAR column; {col!r} is {ctype}")
            return None
        return f"{q(col)} LIKE {lit(cond.value)}"
    values = cond.value if isinstance(cond.value, list) else [cond.value]
    bad = [v for v in values if not _value_fits(v, ctype)]
    if bad:
        ctx.fail(f"value(s) {bad} do not fit column {col!r} ({ctype})")
        return None
    if cond.operator in ("in", "not_in"):
        inner = ", ".join(_typed_lit(v, ctype) for v in values)
        return f"{q(col)} {'IN' if cond.operator == 'in' else 'NOT IN'} ({inner})"
    return f"{q(col)} {_OPS[cond.operator]} {_typed_lit(cond.value, ctype)}"


# ─── steps ─────────────────────────────────────────────────────────────────────

def _step(step, schema: Schema, ctx: _Ctx, warnings: list) -> Optional[tuple[Schema, str]]:
    """One step → (schema after it, the SELECT body over the previous CTE '{prev}')."""
    out = schema.copy()
    schema_case = schema.case
    exprs = {c: q(c) for c in schema.cols}          # column → expression producing it
    where = qualify = ""
    distinct = False

    def need(name, types=None, why=""):
        return _resolve(schema, name, ctx, types, why)

    if isinstance(step, m.Rename):
        renamed = {}
        for old, new in step.mapping.items():
            col = need(old)
            if col is None:
                continue
            renamed[col] = new
        if ctx.problems:
            return None
        cols = {}
        for col, typ in schema.cols.items():
            new = renamed.get(col, col)
            if any(existing.lower() == new.lower() for existing in cols):
                ctx.fail(f"rename would create a duplicate column {new!r}")
                return None
            cols[new] = typ
            exprs_new = q(col) if new == col else f"{q(col)} AS {q(new)}"
            exprs[col] = exprs_new
        out = Schema(cols, {renamed.get(c, c): v for c, v in schema.case.items()})
        select = [exprs[c] for c in schema.cols]
        return out, f"SELECT {', '.join(select)} FROM {{prev}}"

    if isinstance(step, (m.Select, m.Drop)):
        named = [need(c) for c in step.columns]
        if None in named:
            return None
        if len({c.lower() for c in named}) != len(named):
            ctx.fail("a column is listed twice")
            return None
        keep = named if isinstance(step, m.Select) else [c for c in schema.cols if c not in named]
        if not keep:
            ctx.fail("this would leave no columns")
            return None
        out = Schema({c: schema.cols[c] for c in keep})
        return out, f"SELECT {', '.join(q(c) for c in keep)} FROM {{prev}}"

    if isinstance(step, (m.Trim, m.ChangeCase, m.StripChars, m.NullIf)):
        for name in step.columns:
            col = need(name, ("VARCHAR",), step.op)
            if col is None:
                continue
            if isinstance(step, m.Trim):
                expr = f"TRIM({q(col)})"
            elif isinstance(step, m.ChangeCase):
                expr = f"{step.to.upper()}({q(col)})"
                out.case[col] = step.to
            elif isinstance(step, m.StripChars):
                expr = f"translate({q(col)}, {lit(step.chars)}, '')"
            else:
                values = ", ".join(lit(v) for v in step.values)
                expr = f"CASE WHEN {q(col)} IN ({values}) THEN NULL ELSE {q(col)} END"
            exprs[col] = f"{expr} AS {q(col)}"

    elif isinstance(step, m.ReplaceValues):
        whens = " ".join(f"WHEN {lit(a)} THEN {lit(b)}" for a, b in step.mapping.items())
        for name in step.columns:
            col = need(name, ("VARCHAR",), "replace_values")
            if col is not None:
                exprs[col] = f"CASE {q(col)} {whens} ELSE {q(col)} END AS {q(col)}"
                # a key an earlier step made impossible would silently match nothing
                # (live, 2026-10-23: upper-cased in step 2, then mapped "India" in step 3)
                case = schema_case.get(col)
                if case:
                    dead = [k for k in step.mapping if (k != k.upper() if case == "upper" else k != k.lower())]
                    if dead:
                        warnings.append(f"{ctx.where}: mapping key(s) {dead} can never match — an earlier "
                                        f"step made '{col}' {case}-case; write them as "
                                        f"{[k.upper() if case == 'upper' else k.lower() for k in dead]}")

    elif isinstance(step, m.Cast):
        for name, typ in step.columns.items():
            col = need(name)
            if col is None:
                continue
            exprs[col] = f"TRY_CAST({q(col)} AS {typ}) AS {q(col)}"
            out.cols[col] = typ
        if not ctx.problems:
            warnings.append(f"{ctx.where}: cast uses TRY_CAST — values that do not convert become NULL; "
                            f"add a not_null check if that must not happen")

    elif isinstance(step, m.ParseDate):
        for name in step.columns:
            col = need(name, ("VARCHAR",), "parse_date")
            if col is not None:
                tries = ", ".join(f"try_strptime({q(col)}, {lit(f)})" for f in step.formats)
                exprs[col] = f"CAST(COALESCE({tries}) AS {step.output}) AS {q(col)}"
                out.cols[col] = step.output

    elif isinstance(step, m.FillNull):
        for name in step.columns:
            col = need(name)
            if col is None:
                continue
            ctype = schema.cols[col]
            if not _value_fits(step.value, ctype):
                ctx.fail(f"fill value {step.value!r} does not fit column {col!r} ({ctype})")
            else:
                exprs[col] = f"COALESCE({q(col)}, {_typed_lit(step.value, ctype)}) AS {q(col)}"

    elif isinstance(step, m.Filter):
        cond = _condition(step.where, schema, ctx)
        if cond is not None:
            where = f" WHERE {cond}"

    elif isinstance(step, m.Derive):
        if schema.has_new(step.name):
            ctx.fail(f"derive would create {step.name!r}, which already exists")
            return None
        try:
            expr, typ = check_expression(step.expression, schema.cols)
        except SqlCheckError as e:
            ctx.fail(f"derive {step.name!r}: {e}")
            return None
        if step.type:
            expr, typ = f"TRY_CAST({expr} AS {step.type})", step.type
        elif typ == "UNKNOWN":
            ctx.fail(f"derive {step.name!r}: the type of the expression cannot be determined; give 'type'")
            return None
        exprs[step.name] = f"{expr} AS {q(step.name)}"
        out.cols[step.name] = typ

    elif isinstance(step, m.Dedupe):
        if step.key is None:
            distinct = True
        else:
            keys = [need(k) for k in step.key]
            orders = [need(o) for o in step.order_by]
            if None in keys or None in orders:
                return None
            direction = "DESC" if step.keep == "last" else "ASC"
            order = ", ".join(f"{q(o)} {direction} NULLS LAST" for o in orders) if orders \
                else ", ".join(q(k) for k in keys)
            if not orders:
                warnings.append(f"{ctx.where}: dedupe on {keys} has no order_by — which of several rows "
                                f"with the same key is kept is not defined")
            qualify = (f" QUALIFY ROW_NUMBER() OVER (PARTITION BY {', '.join(q(k) for k in keys)} "
                       f"ORDER BY {order}) = 1")

    if ctx.problems:
        return None
    select = ", ".join(exprs[c] for c in out.cols)
    return out, f"SELECT {'DISTINCT ' if distinct else ''}{select} FROM {{prev}}{where}{qualify}"


# ─── checks → Bruin column checks ──────────────────────────────────────────────

def _checks(checks: list, schema: Schema, ctx: _Ctx) -> dict[str, list[dict]]:
    by_col: dict[str, list[dict]] = {}
    for check in checks:
        col = _resolve(schema, check.column, ctx)
        if col is None:
            continue
        ctype = schema.cols[col]
        if check.check in ("positive", "non_negative", "min", "max") and ctype not in NUMERIC:
            ctx.fail(f"check {check.check} needs a numeric column; {col!r} is {ctype}")
            continue
        if check.check == "pattern" and ctype != "VARCHAR":
            ctx.fail(f"check pattern needs a VARCHAR column; {col!r} is {ctype}")
            continue
        if check.check == "accepted_values":
            bad = [v for v in check.value if not _value_fits(v, ctype)]
            if bad:
                ctx.fail(f"accepted_values {bad} do not fit column {col!r} ({ctype})")
                continue
        entry = {"name": check.check}
        if check.value is not None:
            entry["value"] = check.value
        by_col.setdefault(col, []).append(entry)
    return by_col


# ─── compiling a whole plan ────────────────────────────────────────────────────

@dataclass
class Asset:
    name: str                    # raw.orders / clean.orders / mart.by_country
    kind: str                    # raw | clean | aggregate | split | sql
    body: str                    # the SQL Bruin materialises
    depends: list[str]
    columns: list[dict]          # [{"name", "type", "checks": [...]}]
    trace: list[dict] = field(default_factory=list)   # clean only: schema after each step

    def header(self) -> str:
        """The Bruin asset header (YAML inside the @bruin comment)."""
        import yaml
        doc = {"name": self.name, "type": "duckdb.sql", "materialization": {"type": "table"}}
        if self.depends:
            doc["depends"] = list(self.depends)
        cols = []
        for col in self.columns:
            entry = {"name": col["name"], "type": col["type"]}
            if col.get("checks"):
                entry["checks"] = col["checks"]
            cols.append(entry)
        if cols:
            doc["columns"] = cols
        return "/* @bruin\n" + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True) + "@bruin */\n"

    def text(self) -> str:
        return self.header() + self.body + "\n"


@dataclass
class Compiled:
    assets: list[Asset]
    warnings: list[str]
    exports: list[dict]

    def summary(self) -> dict:
        return {
            "assets": {a.name: {"kind": a.kind, "depends": a.depends, "columns": a.columns,
                                "sql": a.body, **({"steps": a.trace} if a.trace else {})}
                       for a in self.assets},
            "warnings": self.warnings,
            "exports": self.exports,
        }


def _columns(schema: Schema, checks: dict) -> list[dict]:
    return [{"name": n, "type": t, **({"checks": checks[n]} if n in checks else {})}
            for n, t in schema.cols.items()]


def compile_plan(plan: m.Plan, source_paths: dict[str, str],
                 source_schemas: dict[str, list[str]]) -> Compiled:
    """
    source_paths: table → absolute server path of its data.csv
    source_schemas: table → column names as DESCRIBE reported them (raw layer: all VARCHAR)
    Raises PlanError listing every problem found.
    """
    problems, warnings = [], []
    schemas: dict[str, Schema] = {}
    assets: list[Asset] = []

    for src in plan.sources:
        names = source_schemas.get(src.table)
        if not names:
            problems.append(f"source {src.table!r}: no schema (the dataset could not be read)")
            continue
        schema = Schema({n: "VARCHAR" for n in names})
        schemas[f"raw.{src.table}"] = schema
        path = source_paths[src.table]
        assets.append(Asset(
            name=f"raw.{src.table}", kind="raw", depends=[], columns=_columns(schema, {}),
            body=f"SELECT * FROM read_csv({lit(path)}, header = true, all_varchar = true)"))

    for table in plan.clean:
        ctx = _Ctx(f"clean.{table.table}")
        schema = schemas.get(f"raw.{table.table}")
        if schema is None:
            problems.append(f"{ctx.where}: its source could not be read")
            continue
        ctes = [f"s0 AS MATERIALIZED (SELECT * FROM raw.{table.table})"]
        trace = [{"step": 0, "op": "source", "columns": schema.as_list()}]
        for i, step in enumerate(table.steps, 1):
            ctx.where = f"clean.{table.table} step {i} ({step.op})"
            result = _step(step, schema, ctx, warnings)
            if result is None:
                break
            schema, body = result
            ctes.append(f"s{i} AS MATERIALIZED ({body.format(prev=f's{i - 1}')})")
            trace.append({"step": i, "op": step.op, "columns": schema.as_list()})
        if ctx.problems:
            problems.extend(ctx.problems)
            continue
        ctx.where = f"clean.{table.table} checks"
        checks = _checks(table.checks, schema, ctx)
        if table.expect_columns is not None:
            want = [(c.name, c.type) for c in table.expect_columns]
            got = list(schema.cols.items())
            if want != got:
                ctx.fail(f"expected columns {want}, but the steps produce {got}")
        if ctx.problems:
            problems.extend(ctx.problems)
            continue
        last = f"s{len(table.steps)}"
        body = "WITH\n" + ",\n".join(ctes) + f"\nSELECT {', '.join(q(c) for c in schema.cols)} FROM {last}"
        ensure_parses(body)
        name = f"clean.{table.table}"
        schemas[name] = schema
        assets.append(Asset(name=name, kind="clean", body=body, depends=[f"raw.{table.table}"],
                            columns=_columns(schema, checks), trace=trace))

    for output in plan.outputs:
        ctx = _Ctx(f"mart.{output.name}")
        if isinstance(output, m.RawSql):
            missing = [s for s in output.sources if s not in schemas]
            if missing:
                ctx.fail(f"sources {missing} are not assets of this plan (known: {sorted(schemas)})")
                problems.extend(ctx.problems)
                continue
            try:
                body, cols = check_query(output.query, {s: schemas[s].cols for s in output.sources})
            except SqlCheckError as e:
                problems.append(f"{ctx.where}: {e}")
                continue
            unknown = [c for c, t in cols if t == "UNKNOWN"]
            if unknown:
                warnings.append(f"{ctx.where}: types of {unknown} could not be determined; "
                                f"they are recorded as UNKNOWN and cannot be used by typed steps")
            schema = Schema(dict(cols))
            checks = _checks(output.checks, schema, ctx)
            if ctx.problems:
                problems.extend(ctx.problems)
                continue
            name = f"mart.{output.name}"
            schemas[name] = schema
            assets.append(Asset(name=name, kind="sql", body=body, depends=list(output.sources),
                                columns=_columns(schema, checks)))
            continue

        source = schemas.get(output.source)
        if source is None:
            ctx.fail(f"source {output.source!r} is not an asset of this plan (known: {sorted(schemas)})")
            problems.extend(ctx.problems)
            continue

        if isinstance(output, m.Aggregate):
            groups = [_resolve(source, g, ctx) for g in output.group_by]
            select, cols = [], {}
            for g in groups:
                if g is not None:
                    select.append(q(g))
                    cols[g] = source.cols[g]
            for measure in output.measures:
                if measure.name.lower() in {c.lower() for c in cols}:
                    ctx.fail(f"measure name {measure.name!r} collides with a column")
                    continue
                if measure.fn == "count" and not measure.column:
                    select.append(f"COUNT(*) AS {q(measure.name)}")
                    cols[measure.name] = "BIGINT"
                    continue
                types = NUMERIC if measure.fn in ("sum", "avg") else None
                col = _resolve(source, measure.column, ctx, types, measure.fn)
                if col is None:
                    continue
                ctype = source.cols[col]
                if measure.fn == "count":
                    expr, typ = f"COUNT({q(col)})", "BIGINT"
                elif measure.fn == "count_distinct":
                    expr, typ = f"COUNT(DISTINCT {q(col)})", "BIGINT"
                elif measure.fn == "sum":
                    # DuckDB sums integers to HUGEINT; keep the declared type instead
                    typ = "DOUBLE" if ctype == "DOUBLE" else "BIGINT"
                    expr = f"CAST(SUM({q(col)}) AS {typ})"
                elif measure.fn == "avg":
                    expr, typ = f"AVG({q(col)})", "DOUBLE"
                else:
                    expr, typ = f"{measure.fn.upper()}({q(col)})", ctype
                select.append(f"{expr} AS {q(measure.name)}")
                cols[measure.name] = typ
            where = ""
            if output.where is not None:
                cond = _condition(output.where, source, ctx)
                where = f" WHERE {cond}" if cond else ""
            schema = Schema(cols)
            checks = _checks(output.checks, schema, ctx)
            if ctx.problems:
                problems.extend(ctx.problems)
                continue
            group = f" GROUP BY {', '.join(q(g) for g in groups)}" if groups else ""
            body = f"SELECT {', '.join(select)} FROM {output.source}{where}{group}"
            ensure_parses(body)
            name = f"mart.{output.name}"
            schemas[name] = schema
            assets.append(Asset(name=name, kind="aggregate", body=body, depends=[output.source],
                                columns=_columns(schema, checks)))

        elif isinstance(output, m.Split):
            key = _resolve(source, output.key, ctx)
            if key is None:
                problems.extend(ctx.problems)
                continue
            warnings.append(f"{ctx.where}: rows are assigned by a hash of {key!r} (seed {output.seed}); "
                            f"rows with a NULL key go to the first part")
            bucket = (f"(hash(COALESCE(CAST({q(key)} AS VARCHAR), '') || ':{output.seed}') % 10000)")
            low = 0
            items = list(output.parts.items())
            for i, (part, frac) in enumerate(items):
                high = 10000 if i == len(items) - 1 else low + round(frac * 10000)
                body = (f"SELECT {', '.join(q(c) for c in source.cols)} FROM {output.source} "
                        f"WHERE {bucket} >= {low} AND {bucket} < {high}")
                ensure_parses(body)
                name = f"mart.{output.name}_{part}"
                schemas[name] = source.copy()
                assets.append(Asset(name=name, kind="split", body=body, depends=[output.source],
                                    columns=_columns(source, {})))
                low = high

    exports = []
    known = {a.name for a in assets}
    for export in plan.exports:
        if export.asset not in known:
            problems.append(f"export {export.asset!r}: not an asset of this plan (known: {sorted(known)})")
        else:
            exports.append({"asset": export.asset, "format": export.format})

    if problems:
        raise PlanError(problems)
    return Compiled(assets=assets, warnings=warnings, exports=exports)
