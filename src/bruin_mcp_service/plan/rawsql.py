"""
SQL the plan supplies itself — a derived-column expression, or a raw-SQL
output — checked against the real schema with sqlglot before anything is
written. Regexes are not used to read SQL.

What is checked, verified against sqlglot 30.24 (2026-10-23):

  * exactly one statement, and it is a query (SELECT / UNION / …), not DDL/DML;
  * every table is one of the upstream assets the plan named (or a CTE the
    query defines) — a table FUNCTION such as read_csv('/etc/passwd') is
    refused, so the query cannot reach files;
  * every function is on the allow-list below (built by parsing approved
    DuckDB calls, so it tracks sqlglot's own names); getenv, glob, read_text,
    current_setting, now(), random() and anything unknown are refused;
  * every column resolves against the upstream schema (sqlglot's qualify with
    validate_qualify_columns) — an invented column is an error;
  * the output columns and, where sqlglot can tell, their types.

Every CTE in an accepted query is marked MATERIALIZED, like the compiler's own
chains: DuckDB 1.5.2 hits an internal binder error ("Failed to bind column
reference … inequal types") on some non-materialized CTE chains with window
functions (reproduced on Bruin v0.11.780, 2026-10-23).
"""

from __future__ import annotations

from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify

DIALECT = "duckdb"

_APPROVED_CALLS = [
    "abs(x)", "round(x, 2)", "floor(x)", "ceil(x)", "sqrt(x)", "power(x, 2)", "ln(x)", "log10(x)",
    "exp(x)", "sign(x)", "coalesce(x, 0)", "nullif(x, 0)", "greatest(x, y)", "least(x, y)",
    "CAST(x AS INT)", "TRY_CAST(x AS INT)", "trim(s)", "upper(s)", "lower(s)", "length(s)",
    "substring(s, 1, 2)", "replace(s, 'a', 'b')", "concat(s, t)", "concat_ws('-', s, t)",
    "regexp_replace(s, 'a', 'b')", "regexp_matches(s, 'a')", "translate(s, 'a', 'b')", "left(s, 2)",
    "right(s, 2)", "lpad(s, 2, '0')", "starts_with(s, 'a')", "contains(s, 'a')", "split_part(s, ',', 1)",
    "strptime(s, '%Y')", "try_strptime(s, '%Y')", "strftime(d, '%Y')", "date_trunc('month', d)",
    "extract(year FROM d)", "year(d)", "month(d)", "day(d)", "quarter(d)", "dayofweek(d)",
    "date_diff('day', d, e)", "date_add(d, INTERVAL 1 DAY)", "sum(x)", "avg(x)", "count(x)",
    "min(x)", "max(x)", "median(x)", "stddev(x)", "variance(x)", "any_value(x)", "arg_min(x, y)",
    "arg_max(x, y)", "count_if(x > 1)", "quantile_cont(x, 0.5)", "string_agg(s, ',')",
    "row_number() OVER ()", "rank() OVER ()", "dense_rank() OVER ()", "lag(x) OVER ()",
    "lead(x) OVER ()", "first_value(x) OVER ()", "ntile(4) OVER ()",
    "CASE WHEN x > 1 THEN 1 ELSE 0 END", "hash(s)", "md5(s)",
    # operators: sqlglot models some of them (AND, OR, …) as function nodes
    "x > 1 AND y < 2 OR NOT z", "x = 1", "x <> 1", "x >= 1", "x <= 1", "x IS NULL", "x IN (1, 2)",
    "x BETWEEN 1 AND 2", "s LIKE 'a%'", "s ILIKE 'a%'", "s || t", "x % 2", "-x", "x / y", "x * y",
    "x - y", "x + y",
]


def _allow_list() -> tuple[frozenset, frozenset]:
    classes, anonymous = set(), set()
    for call in _APPROVED_CALLS:
        node = sqlglot.parse_one(f"SELECT {call} FROM t", read=DIALECT).expressions[0]
        for func in node.find_all(exp.Func):
            if isinstance(func, exp.Anonymous):
                anonymous.add(func.name.lower())
            else:
                classes.add(type(func).__name__)
    return frozenset(classes), frozenset(anonymous)


ALLOWED_FUNCS, ALLOWED_ANONYMOUS = _allow_list()
_AGG_OR_WINDOW = (exp.AggFunc, exp.Window)
_QUERY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

_TYPE_MAP = {
    "BIGINT": "BIGINT", "INT": "INTEGER", "SMALLINT": "INTEGER", "TINYINT": "INTEGER",
    "UBIGINT": "BIGINT", "UINT": "BIGINT", "HUGEINT": "BIGINT",
    "DOUBLE": "DOUBLE", "FLOAT": "DOUBLE", "DECIMAL": "DOUBLE",
    "VARCHAR": "VARCHAR", "TEXT": "VARCHAR", "CHAR": "VARCHAR",
    "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "DATETIME": "TIMESTAMP",
}


class SqlCheckError(ValueError):
    """The SQL was refused. The message says why and is safe to show the client."""


def _norm_type(dtype) -> str:
    if dtype is None:
        return "UNKNOWN"
    name = dtype.this.name if isinstance(dtype, exp.DataType) else str(dtype)
    return _TYPE_MAP.get(str(name).upper(), "UNKNOWN")


def _check_functions(tree: exp.Expression) -> None:
    for func in tree.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):
            if func.name.lower() not in ALLOWED_ANONYMOUS:
                raise SqlCheckError(f"function {func.name}() is not allowed")
        elif type(func).__name__ not in ALLOWED_FUNCS:
            raise SqlCheckError(f"function {func.sql(DIALECT)[:60]} is not allowed")


# The raw layer reads every column as text, and DuckDB 1.5.2 will not do maths
# on text: s * 2, -s, sum(s), round(s, 2), s > 10 all fail at bind time
# ("No function matches … '*(VARCHAR, INTEGER_LITERAL)'") — but only once
# bruin runs. So do s > n and s BETWEEN 1 AND 9 against numbers (s = 1, s <> n
# and s IN (1, 2) do run, comparing as text). A text LITERAL is fine ('5' * 2),
# and so is CAST(s AS INT) * 2. Verified 2026-10-27. sqlglot still types s * 2
# as INT, so the operands are what is checked, not the result.
_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.IntDiv, exp.Mod, exp.Neg)
_NUMERIC_FUNCS = (exp.Sum, exp.Avg, exp.Stddev, exp.StddevPop, exp.StddevSamp, exp.Variance,
                  exp.VariancePop, exp.Abs, exp.Round, exp.Floor, exp.Ceil, exp.Sqrt, exp.Pow,
                  exp.Ln, exp.Log, exp.Exp, exp.Sign)
_ORDERINGS = (exp.GT, exp.GTE, exp.LT, exp.LTE)


def _is_text(node: exp.Expression) -> bool:
    node = node.unnest()
    if isinstance(node, exp.Literal) or node.type is None:
        return False
    return node.type.is_type(*exp.DataType.TEXT_TYPES)


def _is_number(node: exp.Expression) -> bool:
    node = node.unnest()
    return node.type is not None and node.type.is_type(*exp.DataType.NUMERIC_TYPES)


def _shown(node: exp.Expression) -> str:
    # quote a name only when it needs it ("Order ID"), as the plan's author would write it
    bare = node.unnest().transform(
        lambda n: exp.column(exp.to_identifier(n.name)) if isinstance(n, exp.Column) else n)
    return bare.sql(DIALECT)[:60]


def _check_text_maths(typed: exp.Expression) -> None:
    for node in typed.walk():
        if isinstance(node, _ARITHMETIC):
            operands = [node.this] if isinstance(node, exp.Neg) else [node.left, node.right]
        elif isinstance(node, _NUMERIC_FUNCS):
            operands = [node.this]
        elif isinstance(node, _ORDERINGS):
            operands = [node.left] if _is_number(node.right) else [node.right] if _is_number(node.left) else []
        elif isinstance(node, exp.Between):
            operands = [node.this] if _is_number(node.args["low"]) or _is_number(node.args["high"]) else []
        else:
            continue
        for operand in operands:
            if operand is not None and _is_text(operand):
                raise SqlCheckError(
                    f"{_shown(operand)} is text (VARCHAR), so {_shown(node)} would fail when the pipeline "
                    f"runs (DuckDB does no maths on text); make it a number first — a cast step on the "
                    f"column, or TRY_CAST({_shown(operand)} AS DOUBLE)")


def _schema_mapping(schemas: dict[str, dict[str, str]]) -> dict:
    """{'clean.orders': {...}} → sqlglot's nested {'clean': {'orders': {...}}}."""
    mapping: dict = {}
    for asset, cols in schemas.items():
        db, _, table = asset.partition(".")
        mapping.setdefault(db, {})[table] = dict(cols)
    return mapping


def check_expression(expression: str, columns: dict[str, str], row_level: bool = True) -> tuple[str, str]:
    """A derived-column expression over `columns` → (DuckDB SQL, type)."""
    try:
        tree = sqlglot.parse_one(f"SELECT ({expression}) AS _derived FROM _src", read=DIALECT)
    except ParseError as e:
        raise SqlCheckError(f"expression does not parse: {str(e).splitlines()[0][:160]}") from None
    if not isinstance(tree, exp.Select) or len(tree.expressions) != 1 or tree.find(exp.From) is None \
            or tree.find(exp.Where) or tree.find(exp.Group) or tree.find(exp.Join):
        raise SqlCheckError("an expression must be a single value expression")
    if list(tree.find_all(exp.Select))[1:] or list(tree.find_all(exp.Subquery)):
        raise SqlCheckError("subqueries are not allowed in an expression")
    if row_level and any(tree.find_all(*_AGG_OR_WINDOW)):
        raise SqlCheckError("aggregate and window functions are not allowed in a row-level expression "
                            "(use an aggregate output instead)")
    _check_functions(tree)
    written = tree.expressions[0].this.copy()
    try:
        qualified = qualify(tree, schema={"_src": dict(columns)}, dialect=DIALECT,
                            validate_qualify_columns=True, quote_identifiers=True)
    except OptimizeError as e:
        raise SqlCheckError(f"expression refers to a column that does not exist: {e}") from None
    typed = annotate_types(qualified, schema={"_src": dict(columns)}, dialect=DIALECT)
    _check_text_maths(typed)
    # validated on the qualified copy; emitted as written (qualify lower-cases
    # identifiers, DuckDB resolves them case-insensitively either way)
    return written.sql(DIALECT), _norm_type(typed.expressions[0].this.type)


def check_query(query: str, sources: dict[str, dict[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """A raw-SQL output over the named upstream assets → (DuckDB SQL, [(column, type)])."""
    try:
        statements = [s for s in sqlglot.parse(query, read=DIALECT) if s is not None]
    except ParseError as e:
        raise SqlCheckError(f"SQL does not parse: {str(e).splitlines()[0][:160]}") from None
    if len(statements) != 1:
        raise SqlCheckError(f"exactly one statement is allowed, got {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, _QUERY_ROOTS):
        raise SqlCheckError(f"only a query (SELECT) is allowed, got {type(tree).__name__.upper()}")
    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command,
                             exp.Set, exp.Copy, exp.Pragma, exp.Attach, exp.Merge)):
            raise SqlCheckError(f"{type(node).__name__.upper()} is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    allowed = {name.lower() for name in sources}
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Func) or not isinstance(table.this, exp.Identifier):
            raise SqlCheckError(f"table functions are not allowed ({table.sql(DIALECT)[:80]})")
        full = f"{table.db}.{table.name}".lower() if table.db else table.name.lower()
        if not table.db and full in cte_names:
            continue
        if full not in allowed:
            raise SqlCheckError(f"table {full!r} is not one of this output's sources {sorted(sources)}")
    _check_functions(tree)

    mapping = _schema_mapping(sources)
    written = tree.copy()
    try:
        qualified = qualify(tree, schema=mapping, dialect=DIALECT, validate_qualify_columns=True,
                            quote_identifiers=True)
    except OptimizeError as e:
        raise SqlCheckError(f"query refers to a column that does not exist: {e}") from None
    typed = annotate_types(qualified, schema=mapping, dialect=DIALECT)
    _check_text_maths(typed)
    selects = typed.selects if hasattr(typed, "selects") else []
    names = typed.named_selects
    if "*" in names or not names:
        raise SqlCheckError("could not determine the output columns; list them explicitly")
    lowered = [n.lower() for n in names]
    if len(set(lowered)) != len(lowered):
        raise SqlCheckError(f"duplicate output column names: {names}")
    # qualify lower-cased the names; give them back the spelling DuckDB will use
    spelled = {c.lower(): c for cols in sources.values() for c in cols}
    for sel in getattr(written, "selects", []):
        if sel.alias_or_name and sel.alias_or_name != "*":
            spelled[sel.alias_or_name.lower()] = sel.alias_or_name
    out = [(spelled.get(name.lower(), name), _norm_type(getattr(sel, "type", None)))
           for name, sel in zip(names, selects)]

    for cte in written.find_all(exp.CTE):
        cte.set("materialized", True)
    return written.sql(DIALECT), out


def ensure_parses(sql: str) -> None:
    """Internal check that SQL the compiler produced is one valid DuckDB statement."""
    statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    if len(statements) != 1:
        raise AssertionError(f"compiler produced {len(statements)} statements")


def first_error_line(err: Exception) -> Optional[str]:
    return str(err).splitlines()[0] if str(err) else None
