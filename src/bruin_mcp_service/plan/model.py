"""
The plan format, version 1.

A plan names its sources (uploaded datasets), how each source table is
cleaned (an ordered list of steps), optional further outputs, quality checks,
and what should be exported. It says WHAT to do; engine.py decides the SQL.

Every model forbids unknown fields, so a typo is an error rather than a step
silently ignored. Column names coming from data may be anything ("Order ID");
names the plan creates (renames, derived columns, outputs) must be snake_case,
because DuckDB identifiers are case-insensitive and quoting rules differ
between tools.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME = r"^[a-z][a-z0-9_]{0,62}$"
_NAME = re.compile(NAME)

# Types a plan may produce. Kept small on purpose: each one is tested.
SQL_TYPES = ("VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP")
SqlType = Literal["VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP"]
Scalar = Union[str, int, float, bool]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ColumnsStep(_Strict):
    """Every step that works on columns takes `columns` (a list); a single `column` is accepted too."""

    @model_validator(mode="before")
    @classmethod
    def _column_alias(cls, data):
        if isinstance(data, dict) and "column" in data:
            if "columns" in data:
                raise ValueError("give either 'columns' or 'column', not both")
            data = {**data, "columns": [data["column"]]}
            del data["column"]
        return data


def _new_name(value: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise ValueError(f"{value!r} is not a valid new name (snake_case: a-z, 0-9, _; starts with a letter)")
    return value


# ─── conditions (filter steps, aggregate where) ────────────────────────────────

class Compare(_Strict):
    column: str
    operator: Literal["eq", "ne", "gt", "ge", "lt", "le", "in", "not_in", "is_null", "not_null", "like"]
    value: Optional[Union[Scalar, list[Scalar]]] = None

    @model_validator(mode="after")
    def _value_matches_operator(self):
        if self.operator in ("is_null", "not_null"):
            if self.value is not None:
                raise ValueError(f"{self.operator} takes no value")
        elif self.operator in ("in", "not_in"):
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.operator} needs a non-empty list value")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"{self.operator} needs a single value")
        if self.operator == "like" and not isinstance(self.value, str):
            raise ValueError("like needs a string pattern")
        return self


class AllOf(_Strict):
    all: list["Condition"] = Field(min_length=1)


class AnyOf(_Strict):
    any: list["Condition"] = Field(min_length=1)


Condition = Union[Compare, AllOf, AnyOf]
AllOf.model_rebuild()
AnyOf.model_rebuild()


# ─── cleaning steps ────────────────────────────────────────────────────────────

class Rename(_Strict):
    """Rename columns: mapping {old name: new snake_case name}."""
    op: Literal["rename"]
    mapping: dict[str, str] = Field(min_length=1)

    @field_validator("mapping")
    @classmethod
    def _targets(cls, v):
        for new in v.values():
            _new_name(new)
        return v


class Select(_ColumnsStep):
    """Keep only these columns, in this order."""
    op: Literal["select"]
    columns: list[str] = Field(min_length=1)


class Drop(_ColumnsStep):
    """Remove these columns."""
    op: Literal["drop"]
    columns: list[str] = Field(min_length=1)


class Trim(_ColumnsStep):
    """Remove leading/trailing whitespace (text columns)."""
    op: Literal["trim"]
    columns: list[str] = Field(min_length=1)


class ChangeCase(_ColumnsStep):
    """Upper- or lower-case text columns."""
    op: Literal["change_case"]
    columns: list[str] = Field(min_length=1)
    to: Literal["upper", "lower"]


class ReplaceValues(_ColumnsStep):
    """Replace exact values in text columns: mapping {from: to}."""
    op: Literal["replace_values"]
    columns: list[str] = Field(min_length=1)
    mapping: dict[str, str] = Field(min_length=1)


class StripChars(_ColumnsStep):
    """Delete every listed character (e.g. "$,") from text columns."""
    op: Literal["strip_chars"]
    columns: list[str] = Field(min_length=1)
    chars: str = Field(min_length=1, max_length=32)


class NullIf(_ColumnsStep):
    """Turn the listed values (e.g. "", "n/a") into NULL in text columns."""
    op: Literal["null_if"]
    columns: list[str] = Field(min_length=1)
    values: list[str] = Field(min_length=1)


class Cast(_Strict):
    """TRY_CAST: a value that does not convert becomes NULL (add a not_null check to catch that)."""
    op: Literal["cast"]
    columns: dict[str, SqlType] = Field(min_length=1)


class ParseDate(_ColumnsStep):
    """Explicit formats only (strftime, e.g. '%d/%m/%Y'): the plan states the day/month order."""
    op: Literal["parse_date"]
    columns: list[str] = Field(min_length=1)
    formats: list[str] = Field(min_length=1, max_length=8)
    output: Literal["DATE", "TIMESTAMP"] = "DATE"

    @field_validator("formats")
    @classmethod
    def _has_directives(cls, v):
        for fmt in v:
            if "%" not in fmt:
                raise ValueError(f"format {fmt!r} has no strftime directive")
        return v


class FillNull(_ColumnsStep):
    """Replace NULL with a value that fits the column type."""
    op: Literal["fill_null"]
    columns: list[str] = Field(min_length=1)
    value: Scalar


class Filter(_Strict):
    """Keep only rows matching the condition."""
    op: Literal["filter"]
    where: Condition


class Derive(_Strict):
    """A new column from a SQL expression over existing columns (checked with sqlglot)."""
    op: Literal["derive"]
    name: str
    expression: str = Field(min_length=1, max_length=2000)
    type: Optional[SqlType] = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v):
        return _new_name(v)


class Dedupe(_Strict):
    """key=None removes exact duplicate rows; otherwise one row per key, chosen by order_by."""
    op: Literal["dedupe"]
    key: Optional[list[str]] = Field(default=None, min_length=1)
    order_by: list[str] = Field(default_factory=list)
    keep: Literal["first", "last"] = "first"

    @model_validator(mode="after")
    def _order_needs_key(self):
        if self.key is None and self.order_by:
            raise ValueError("order_by only applies when deduplicating on a key")
        return self


Step = Annotated[Union[Rename, Select, Drop, Trim, ChangeCase, ReplaceValues, StripChars, NullIf,
                       Cast, ParseDate, FillNull, Filter, Derive, Dedupe], Field(discriminator="op")]


# ─── checks, tables, outputs ───────────────────────────────────────────────────

class Check(_Strict):
    """Becomes a Bruin column check on the asset (run by `bruin run`)."""
    column: str
    check: Literal["not_null", "unique", "accepted_values", "positive", "non_negative", "min", "max", "pattern"]
    value: Optional[Union[Scalar, list[Scalar]]] = None

    @model_validator(mode="after")
    def _value(self):
        needs = {"accepted_values": list, "min": (int, float), "max": (int, float), "pattern": str}
        want = needs.get(self.check)
        if want is None and self.value is not None:
            raise ValueError(f"{self.check} takes no value")
        if want is not None and not isinstance(self.value, want):
            raise ValueError(f"{self.check} needs a value of type {want}")
        return self


class ColumnSpec(_Strict):
    name: str
    type: SqlType


class Source(_Strict):
    table: str
    dataset_id: str

    @field_validator("table")
    @classmethod
    def _valid_table(cls, v):
        return _new_name(v)


class CleanTable(_Strict):
    """One cleaning asset (clean.<table>) for one source table."""
    table: str
    steps: list[Step] = Field(default_factory=list, max_length=60)
    checks: list[Check] = Field(default_factory=list)
    expect_columns: Optional[list[ColumnSpec]] = None


class Measure(_Strict):
    name: str
    fn: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    column: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v):
        return _new_name(v)

    @model_validator(mode="after")
    def _column(self):
        if self.fn != "count" and not self.column:
            raise ValueError(f"{self.fn} needs a column")
        return self


class Aggregate(_Strict):
    kind: Literal["aggregate"]
    name: str
    source: str                       # 'clean.<table>' or 'mart.<name>'
    group_by: list[str] = Field(default_factory=list)
    measures: list[Measure] = Field(min_length=1)
    where: Optional[Condition] = None
    checks: list[Check] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v):
        return _new_name(v)


class Split(_Strict):
    """Deterministic hash split on a key column; only when the user asked for one."""
    kind: Literal["split"]
    name: str
    source: str
    key: str
    parts: dict[str, float] = Field(min_length=2, max_length=4)
    seed: int = 42

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v):
        return _new_name(v)

    @field_validator("parts")
    @classmethod
    def _fractions(cls, v):
        for part, frac in v.items():
            _new_name(part)
            if not 0 < frac < 1:
                raise ValueError(f"fraction for {part!r} must be between 0 and 1")
        if abs(sum(v.values()) - 1.0) > 1e-9:
            raise ValueError(f"fractions must add up to 1, got {sum(v.values())}")
        return v


class RawSql(_Strict):
    """Fallback for what steps cannot express. One SELECT over the listed upstream assets."""
    kind: Literal["sql"]
    name: str
    sources: list[str] = Field(min_length=1)
    query: str = Field(min_length=1, max_length=20000)
    checks: list[Check] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v):
        return _new_name(v)


Output = Annotated[Union[Aggregate, Split, RawSql], Field(discriminator="kind")]


class Export(_Strict):
    asset: str
    format: Literal["csv", "xlsx"] = "csv"


class Plan(_Strict):
    version: Literal[1] = 1

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, v):
        # "1" or 1.0 from a model's JSON is still version 1
        return 1 if str(v).strip() in ("1", "1.0") else v
    name: str
    description: str = Field(default="", max_length=2000)
    sources: list[Source] = Field(min_length=1, max_length=10)
    clean: list[CleanTable] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list, max_length=20)
    exports: list[Export] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v):
        return _new_name(v)

    @model_validator(mode="after")
    def _references(self):
        tables = [s.table for s in self.sources]
        if len(set(tables)) != len(tables):
            raise ValueError("source table names must be unique")
        cleaned = [c.table for c in self.clean]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("each source table is cleaned at most once (one cleaning asset per table)")
        unknown = set(cleaned) - set(tables)
        if unknown:
            raise ValueError(f"clean refers to unknown source table(s): {sorted(unknown)}")
        names = [o.name for o in self.outputs]
        if len(set(names)) != len(names):
            raise ValueError("output names must be unique")
        return self


def parse_plan(data: Any) -> Plan:
    """Validate the plan's FORMAT (pydantic). Schema checks happen in engine.py.

    A JSON string is accepted as well as an object: models often send nested
    arguments that way.
    """
    if isinstance(data, str):
        import json
        try:
            data = json.loads(data)
        except ValueError as e:
            raise ValueError(f"the plan is a string but not valid JSON: {e}") from None
    return Plan.model_validate(data)


def step_fields() -> dict[str, dict[str, str]]:
    """For each step op: its fields and their JSON types, from the models themselves."""
    out = {}
    for model in (Rename, Select, Drop, Trim, ChangeCase, ReplaceValues, StripChars, NullIf, Cast,
                  ParseDate, FillNull, Filter, Derive, Dedupe):
        schema = model.model_json_schema()
        op = schema["properties"]["op"].get("const") or schema["properties"]["op"].get("enum", ["?"])[0]
        required = set(schema.get("required", []))
        out[op] = {name: ("required" if name in required else "optional")
                   for name in schema["properties"] if name != "op"}
    return out


EXAMPLE_PLAN = {
    "version": 1,
    "name": "orders_prep",
    "sources": [{"table": "orders", "dataset_id": "<dataset_id from upload_dataset>"}],
    "clean": [{
        "table": "orders",
        "steps": [
            {"op": "rename", "mapping": {"Order ID": "order_id"}},
            {"op": "trim", "columns": ["customer", "country", "amount"]},
            {"op": "change_case", "columns": ["country"], "to": "upper"},
            {"op": "replace_values", "columns": ["country"], "mapping": {"USA": "US", "INDIA": "IN"}},
            {"op": "null_if", "columns": ["amount"], "values": ["", "n/a"]},
            {"op": "strip_chars", "columns": ["amount"], "chars": "$,"},
            {"op": "cast", "columns": {"order_id": "BIGINT", "amount": "DOUBLE"}},
            {"op": "parse_date", "columns": ["order_date"], "formats": ["%Y-%m-%d", "%d/%m/%Y"]},
            {"op": "dedupe", "key": ["order_id"], "order_by": ["order_date"], "keep": "last"},
            {"op": "filter", "where": {"column": "amount", "operator": "ge", "value": 0}},
        ],
        "checks": [{"column": "order_id", "check": "not_null"}, {"column": "order_id", "check": "unique"}],
    }],
    "outputs": [{"kind": "aggregate", "name": "revenue_by_country", "source": "clean.orders",
                 "group_by": ["country"], "measures": [{"name": "orders", "fn": "count"},
                                                       {"name": "revenue", "fn": "sum", "column": "amount"}]}],
    "exports": [{"asset": "clean.orders", "format": "csv"}],
}


def plan_format() -> dict:
    """The format reference a client (and its model) needs to write a valid plan."""
    return {
        "top_level": {"version": "1", "name": "snake_case", "description": "optional",
                      "sources": "[{table: snake_case, dataset_id}]",
                      "clean": "[{table, steps: [...], checks: [...], expect_columns?: [{name, type}]}] — "
                               "one entry per source table to clean",
                      "outputs": "[aggregate | split | sql]", "exports": "[{asset, format: csv|xlsx}]"},
        "steps": step_fields(),
        "step_notes": {
            "all": "every source column starts as VARCHAR; each step sees only the previous step's columns",
            "cast": "columns is an object {column: TYPE}; TRY_CAST, so failures become NULL",
            "parse_date": "formats are strftime patterns tried in order; they state the day/month order",
            "dedupe": "no key: exact duplicate rows; key: one row per key, ordered by order_by, keep first|last",
            "derive": "expression is a SQL expression over existing columns; give type if it cannot be inferred",
            "filter": "where is a condition (below)",
        },
        "types": list(SQL_TYPES),
        "condition": "{column, operator: eq|ne|gt|ge|lt|le|in|not_in|is_null|not_null|like, value} "
                     "or {all: [conditions]} or {any: [conditions]}",
        "checks": "{column, check: not_null|unique|accepted_values|positive|non_negative|min|max|pattern, value?}",
        "outputs": {
            "aggregate": "{kind: aggregate, name, source: 'clean.<table>', group_by: [...], "
                         "measures: [{name, fn: count|count_distinct|sum|avg|min|max, column?}], where?}",
            "split": "{kind: split, name, source, key, parts: {train: 0.8, test: 0.2}, seed?} — only if asked for",
            "sql": "{kind: sql, name, sources: ['clean.<table>'], query: 'SELECT …'} — fallback; one SELECT",
        },
        "asset_names": "raw.<table>, clean.<table>, mart.<output name> (split parts: mart.<name>_<part>)",
        "example": EXAMPLE_PLAN,
    }


class TablePlan(_Strict):
    """
    The flat, single-table form: one dataset, its cleaning steps, checks and
    outputs as top-level fields. It is converted to an ordinary Plan
    (to_plan) and goes through the same engine — it is a shorter way to write
    the same thing, with two fewer levels of nesting.
    """
    dataset_id: str = Field(description="from upload_dataset")
    table: str = Field(default="data", description="snake_case name; assets become raw.<table> and clean.<table>")
    name: Optional[str] = Field(default=None, description="snake_case pipeline name (default: <table>_prep)")
    description: str = Field(default="", max_length=2000)
    steps: list[Step] = Field(default_factory=list, max_length=60, description="cleaning steps, applied in order")
    checks: list[Check] = Field(default_factory=list, description="quality checks on clean.<table>")
    expect_columns: Optional[list[ColumnSpec]] = None
    outputs: list[Output] = Field(default_factory=list, max_length=20,
                                  description="optional extra results; source is 'clean.<table>'")
    exports: list[Export] = Field(default_factory=list)

    @field_validator("table")
    @classmethod
    def _valid_table(cls, v):
        return _new_name(v)

    def to_plan(self) -> Plan:
        return Plan.model_validate({
            "version": 1,
            "name": self.name or f"{self.table}_prep",
            "description": self.description,
            "sources": [{"table": self.table, "dataset_id": self.dataset_id}],
            "clean": [{"table": self.table, "steps": [s.model_dump(mode="json", exclude_none=False) for s in self.steps],
                       "checks": [c.model_dump(mode="json", exclude_none=True) for c in self.checks],
                       **({"expect_columns": [c.model_dump(mode="json") for c in self.expect_columns]}
                          if self.expect_columns is not None else {})}],
            "outputs": [o.model_dump(mode="json", exclude_none=True) for o in self.outputs],
            "exports": [e.model_dump(mode="json") for e in self.exports],
        })


def parse_table_plan(arguments: dict) -> Plan:
    """The flat form's tool arguments → an ordinary Plan (strict; nothing repaired)."""
    return TablePlan.model_validate(arguments).to_plan()
