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
import re
from dataclasses import dataclass, field
from typing import Optional

from . import model as m
from .rawsql import SqlCheckError, case_null_warnings, check_expression, check_query, ensure_parses

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
        self.changed: dict[str, str] = {}      # set by _step: column → the expression giving its new value

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


def _not_assets(names: list[str], known, planned: set[str], as_list: bool = False) -> str:
    """Unknown asset names, each with its likeliest meaning BEFORE the (long) known list: a
    live client's view of the message was cut inside that list, so the hint never showed.
    `planned` are outputs of this plan defined later, which an earlier output cannot use."""
    hints = []
    for name in names:
        near = difflib.get_close_matches(name, sorted(set(known) | planned), n=1, cutoff=0.6)
        if near and near[0] in known:
            hints.append(f"did you mean {near[0]!r} for {name!r}?")
        elif near:
            hints.append(f"{near[0]!r} (for {name!r}?) is defined later in outputs; list it before this output")
    what = f"{names} are not assets" if as_list else f"{names[0]!r} is not an asset"
    return f"{what} of this plan{' — ' + ' '.join(hints) if hints else ''} (known: {sorted(known)})"


# ─── conditions ────────────────────────────────────────────────────────────────

_OPS = {"eq": "=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}


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
    # ne / not_in keep missing values: "not cancelled" must not also drop rows whose
    # status is unknown (plain SQL <> and NOT IN do — a live run lost 12 of 60 rows)
    if cond.operator in ("in", "not_in"):
        inner = ", ".join(_typed_lit(v, ctype) for v in values)
        if cond.operator == "in":
            return f"{q(col)} IN ({inner})"
        return f"({q(col)} NOT IN ({inner}) OR {q(col)} IS NULL)"
    if cond.operator == "ne":
        return f"{q(col)} IS DISTINCT FROM {_typed_lit(cond.value, ctype)}"
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
        to_integer = []
        for name, typ in step.columns.items():
            col = need(name)
            if col is None:
                continue
            if typ in ("INTEGER", "BIGINT") and schema.cols[col] in ("VARCHAR", "DOUBLE"):
                # TRY_CAST rounds ('3.5' → 4, '-2.5' → -3); a value with a fraction becomes
                # NULL instead, a whole one ('3.0', '1e3') converts. (Past 2^53 a DOUBLE holds
                # no fraction, so none can be seen there.)
                as_double = f"TRY_CAST({q(col)} AS DOUBLE)"
                exprs[col] = (f"CASE WHEN {as_double} = floor({as_double}) THEN TRY_CAST({q(col)} AS {typ}) END "
                              f"AS {q(col)}")
                to_integer.append(col)
            else:
                exprs[col] = f"TRY_CAST({q(col)} AS {typ}) AS {q(col)}"
            out.cols[col] = typ
        if not ctx.problems:
            fraction = (f"; for {to_integer} (to a whole-number type) that includes numbers with a fraction "
                        f"like 3.5, which are not rounded — cast to DOUBLE to keep them") if to_integer else ""
            warnings.append(f"{ctx.where}: cast uses TRY_CAST — values that do not convert become NULL{fraction}; "
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
        for warning in case_null_warnings(step.expression):
            warnings.append(f"{ctx.where}: derive {step.name!r}: {warning}")
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
    # columns this step rewrites in place (not new ones): changes.py counts what each did
    ctx.changed = {c: exprs[c][:-len(f" AS {q(c)}")] for c in schema.cols
                   if c in out.cols and exprs[c] != q(c) and exprs[c].endswith(f" AS {q(c)}")}
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
    origins: dict = field(default_factory=dict)       # clean only: column → the raw column it came from (None: new)

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
    # per cleaned table, a query that checks replace_values keys against the data (run by validate)
    mapping_probes: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "assets": {a.name: {"kind": a.kind, "depends": a.depends, "columns": a.columns,
                                "sql": a.body, **({"steps": a.trace} if a.trace else {}),
                                **({"origins": a.origins} if a.kind == "clean" else {})}
                       for a in self.assets},
            "warnings": self.warnings,
            "exports": self.exports,
        }


def _mapping_probe(table: str, path: str, ctes: list[str], mapped: list) -> dict:
    """One query: for each replace_values key, does any value equal it AT THAT STEP?
    And which values does the column hold there (if it holds at most LEFTOVER_MAX)?

    It runs the plan's own CTE chain up to the step before each mapping, over the
    real data, so earlier trims / case changes count exactly as they will in the
    run. A live run mapped 'disloyal customer' where the data said 'disloyal
    Customer': validation passed, the run "replaced" nothing, and the summary
    said it had. Another mapped 'Undergraduate' and left 'undergrad' behind.
    Returns rows (kind, step, column, text, n, near): kind 'key' — text a key,
    n its hits, near a value equal to it ignoring case and surrounding spaces;
    kind 'value' — text a value of the column, n its rows.
    """
    last = max(i for i, *_ in mapped)
    chain = [f"s0 AS MATERIALIZED (SELECT * FROM read_csv({lit(path)}, header = true, all_varchar = true))",
             *ctes[1:last]]
    parts = []
    for i, _, col, mapping in mapped:
        values = ", ".join(f"({lit(k)})" for k in mapping)
        parts.append(
            f"SELECT 'key' AS kind, {i} AS step, {lit(col)} AS col, k.mkey AS text, "
            f"count(*) FILTER (WHERE v.c = k.mkey) AS n, "
            f"min(v.c) FILTER (WHERE lower(trim(v.c)) = lower(trim(k.mkey))) AS near "
            f"FROM (VALUES {values}) k(mkey) CROSS JOIN (SELECT DISTINCT {q(col)} AS c FROM s{i - 1}) v "
            f"GROUP BY k.mkey")
        parts.append(
            f"SELECT 'value', {i}, {lit(col)}, c, n, NULL FROM (SELECT {q(col)} AS c, count(*) AS n FROM s{i - 1} "
            f"WHERE {q(col)} IS NOT NULL GROUP BY 1 QUALIFY count(*) OVER () <= {LEFTOVER_MAX})")
    sql = "WITH\n" + ",\n".join(chain) + "\n" + "\nUNION ALL\n".join(parts) + "\nORDER BY kind, step, col, text"
    ensure_parses(sql)
    return {"table": table, "path": path, "sql": sql, "steps": {i: where for i, where, *_ in mapped},
            "mappings": {(i, col): mapping for i, _, col, mapping in mapped}}


LEFTOVER_MAX = 200          # a column with more distinct values is not a category list; not checked


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def looks_like(value: str, other: str) -> bool:
    """
    'undergrad' ~ 'Undergraduate', 'Bachelors' ~ "bachelor's", 'Mastres' ~ 'Masters'.
    Spelling slips keep the first letter; 'Loyal' ~ 'disloyal' (live: 0.90 alike) is the opposite.
    """
    a, b = _norm(value), _norm(other)
    if not a or not b:
        return False
    short, long = sorted((a, b), key=len)
    return a == b or (len(short) >= 4 and long.startswith(short)) or \
        (a[0] == b[0] and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)


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
    probes: list[dict] = []
    # assets already attempted: one of them missing from `schemas` failed above, which
    # an output using it must say (live: "not an asset of this plan" read like a typo)
    attempted: set[str] = set()

    def unbuilt(name: str) -> bool:
        return name in attempted and name not in schemas

    for src in plan.sources:
        attempted.add(f"raw.{src.table}")
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
        attempted.add(ctx.where)
        schema = schemas.get(f"raw.{table.table}")
        if schema is None:
            problems.append(f"{ctx.where}: its source could not be read")
            continue
        ctes = [f"s0 AS MATERIALIZED (SELECT * FROM raw.{table.table})"]
        trace = [{"step": 0, "op": "source", "columns": schema.as_list()}]
        mapped = []                                     # (step, where, column at that step, mapping)
        origins = {c: c for c in schema.cols}
        for i, step in enumerate(table.steps, 1):
            ctx.where = f"clean.{table.table} step {i} ({step.op})"
            renamed = {schema.find(o): n for o, n in step.mapping.items()} if isinstance(step, m.Rename) else {}
            ctx.changed = {}
            if isinstance(step, m.ReplaceValues):
                mapped += [(i, ctx.where, col, dict(step.mapping)) for col in map(schema.find, step.columns)
                           if col is not None and schema.cols[col] == "VARCHAR"]
            result = _step(step, schema, ctx, warnings)
            if result is None:
                break
            back = {new: old for old, new in renamed.items()}
            origins = {c: origins.get(back.get(c, c)) for c in result[0].cols}
            schema, body = result
            ctes.append(f"s{i} AS MATERIALIZED ({body.format(prev=f's{i - 1}')})")
            trace.append({"step": i, "op": step.op, "columns": schema.as_list(),
                          **({"changed": ctx.changed} if ctx.changed else {})})
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
                            columns=_columns(schema, checks), trace=trace, origins=origins))
        if mapped:
            probes.append(_mapping_probe(table.table, source_paths[table.table], ctes, mapped))

    planned = {f"mart.{o.name}_{part}" for o in plan.outputs if isinstance(o, m.Split) for part in o.parts} | \
              {f"mart.{o.name}" for o in plan.outputs if not isinstance(o, m.Split)}
    for output in plan.outputs:
        ctx = _Ctx(f"mart.{output.name}")
        attempted.add(ctx.where)
        if isinstance(output, m.RawSql):
            missing = [s for s in output.sources if s not in schemas]
            failed = [s for s in missing if unbuilt(s)]
            unknown = [s for s in missing if s not in failed]
            if failed:
                ctx.fail(f"sources {failed} were not built because of the problem(s) reported above for them")
            if unknown:
                ctx.fail("sources " + _not_assets(unknown, schemas, planned - {ctx.where}, as_list=True))
            if missing:
                problems.extend(ctx.problems)
                continue
            try:
                body, cols = check_query(output.query, {s: schemas[s].cols for s in output.sources})
            except SqlCheckError as e:
                problems.append(f"{ctx.where}: {e}")
                continue
            warnings.extend(f"{ctx.where}: {w}" for w in case_null_warnings(output.query))
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
            if unbuilt(output.source):
                ctx.fail(f"source {output.source!r} was not built because of the problem(s) reported above for it")
            else:
                ctx.fail("source " + _not_assets([output.source], schemas, planned - {ctx.where}))
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
            problems.append("export " + _not_assets([export.asset], known, set()))
        else:
            exports.append({"asset": export.asset, "format": export.format})

    if problems:
        raise PlanError(problems)
    return Compiled(assets=assets, warnings=warnings, exports=exports, mapping_probes=probes)
