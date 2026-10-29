"""The plan engine without Bruin: format, schema flow, SQL checks, saved plans."""

import pytest
from pydantic import ValidationError

from bruin_mcp_service.plan.engine import PlanError, compile_plan
from bruin_mcp_service.plan.model import parse_plan
from bruin_mcp_service.plan.rawsql import SqlCheckError, check_expression, check_query
from bruin_mcp_service.plan.saved import SavedPlanError, instantiate, list_saved

COLS = ["Order ID", "customer", "country", "amount", "Order Date"]


def _plan(steps=(), checks=(), outputs=(), exports=(), expect=None, **extra):
    clean = {"table": "orders", "steps": list(steps), "checks": list(checks)}
    if expect is not None:
        clean["expect_columns"] = expect
    return parse_plan({"name": "t", "sources": [{"table": "orders", "dataset_id": "ds_0000000000000000"}],
                       "clean": [clean], "outputs": list(outputs), "exports": list(exports), **extra})


def _compile(plan, cols=COLS):
    return compile_plan(plan, {"orders": "/srv/x/data.csv"}, {"orders": list(cols)})


def _problems(plan, cols=COLS):
    with pytest.raises(PlanError) as e:
        _compile(plan, cols)
    return e.value.problems


def _clean(compiled):
    return next(a for a in compiled.assets if a.name == "clean.orders")


# ─── format ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"name": "Bad Name", "sources": [{"table": "o", "dataset_id": "x"}]},
    {"name": "t", "sources": []},
    {"name": "t", "sources": [{"table": "o", "dataset_id": "x"}], "clean": [{"table": "o", "steps": [{"op": "explode"}]}]},
    {"name": "t", "sources": [{"table": "o", "dataset_id": "x"}],
     "clean": [{"table": "o", "steps": [{"op": "trim", "columns": ["a"], "colour": 1}]}]},
    {"name": "t", "sources": [{"table": "o", "dataset_id": "x"}], "clean": [{"table": "other"}]},
    {"name": "t", "sources": [{"table": "o", "dataset_id": "x"}], "clean": [{"table": "o"}, {"table": "o"}]},
    {"name": "t", "sources": [{"table": "o", "dataset_id": "x"}], "clean": [{"table": "o", "steps": [
        {"op": "rename", "mapping": {"a": "Not Snake"}}]}]},
    {"name": "t", "sources": [{"table": "o", "dataset_id": "x"}], "clean": [{"table": "o", "steps": [
        {"op": "parse_date", "column": "d", "formats": ["YYYY-MM-DD"]}]}]},
    {"name": "t", "sources": [{"table": "o", "dataset_id": "x"}], "outputs": [
        {"kind": "split", "name": "s", "source": "clean.o", "key": "k", "parts": {"train": 0.7, "test": 0.2}}]},
])
def test_malformed_plans_are_rejected(bad):
    with pytest.raises(ValidationError):
        parse_plan(bad)


def test_a_cleaning_asset_per_table_as_one_cte_chain():
    c = _compile(_plan([{"op": "trim", "columns": ["customer"]},
                        {"op": "dedupe", "key": ["Order ID"]}]))
    assert [a.name for a in c.assets] == ["raw.orders", "clean.orders"]
    body = _clean(c).body
    assert body.startswith("WITH\ns0 AS MATERIALIZED (SELECT * FROM raw.orders)")
    after_source = body.split("),\n", 1)[1]           # everything after the s0 pass-through
    assert body.count("AS MATERIALIZED") == 3 and "SELECT *" not in after_source


# ─── schema flow ──────────────────────────────────────────────────────────────

def test_invented_columns_are_rejected_with_a_suggestion():
    problems = _problems(_plan([{"op": "trim", "columns": ["custmer"]}]))
    assert "step 1 (trim)" in problems[0] and "'custmer' does not exist" in problems[0]
    assert "did you mean 'customer'" in problems[0]


def test_a_renamed_column_is_gone_under_its_old_name():
    problems = _problems(_plan([{"op": "rename", "mapping": {"Order ID": "order_id"}},
                                {"op": "dedupe", "key": ["Order ID"]}]))
    assert "step 2 (dedupe)" in problems[0] and "'Order ID' does not exist" in problems[0]


def test_derived_and_renamed_columns_are_usable_later():
    c = _compile(_plan([{"op": "rename", "mapping": {"Order ID": "order_id"}},
                        {"op": "cast", "columns": {"amount": "DOUBLE"}},
                        {"op": "derive", "name": "with_tax", "expression": "amount * 1.18"},
                        {"op": "filter", "where": {"column": "with_tax", "operator": "gt", "value": 10}}]))
    trace = _clean(c).trace
    assert [s["op"] for s in trace] == ["source", "rename", "cast", "derive", "filter"]
    assert {"name": "with_tax", "type": "DOUBLE"} in trace[-1]["columns"]


def test_a_derived_name_cannot_shadow_an_existing_column():
    problems = _problems(_plan([{"op": "derive", "name": "customer", "expression": "upper(customer)"}]))
    assert "already exists" in problems[0]


def test_rename_cannot_collide_case_insensitively():
    problems = _problems(_plan([{"op": "rename", "mapping": {"Order ID": "customer"}}]))
    assert "duplicate column 'customer'" in problems[0]


@pytest.mark.parametrize("step,needle", [
    ({"op": "trim", "columns": ["amount"]}, None),                                   # still VARCHAR: fine
    ({"op": "cast", "columns": {"amount": "DOUBLE"}}, None),
])
def test_text_steps_need_text_columns(step, needle):
    c = _compile(_plan([step]))
    assert c.assets


def test_type_errors_are_reported():
    after_cast = [{"op": "cast", "columns": {"amount": "DOUBLE"}}]
    assert "needs VARCHAR" in _problems(_plan(after_cast + [{"op": "trim", "columns": ["amount"]}]))[0]
    assert "does not fit" in _problems(_plan(after_cast + [{"op": "fill_null", "column": "amount", "value": "zero"}]))[0]
    assert "not fit" in _problems(_plan(after_cast + [
        {"op": "filter", "where": {"column": "amount", "operator": "gt", "value": "ten"}}]))[0]
    assert "needs VARCHAR" in _problems(_plan(after_cast + [
        {"op": "parse_date", "column": "amount", "formats": ["%Y"]}]))[0]


def test_select_drop_and_empty_results():
    c = _compile(_plan([{"op": "select", "columns": ["customer", "amount"]}]))
    assert [col["name"] for col in _clean(c).columns] == ["customer", "amount"]
    assert "no columns" in _problems(_plan([{"op": "drop", "columns": COLS}]))[0]


def test_expected_output_columns_must_match():
    good = [{"name": n, "type": "VARCHAR"} for n in COLS]
    assert _compile(_plan(expect=good))
    problems = _problems(_plan(expect=good[:-1]))
    assert "expected columns" in problems[0]


def test_checks_are_validated_and_become_bruin_column_checks():
    c = _compile(_plan([{"op": "cast", "columns": {"amount": "DOUBLE"}}],
                       checks=[{"column": "amount", "check": "non_negative"},
                               {"column": "country", "check": "accepted_values", "value": ["IN", "US"]}]))
    header = _clean(c).header()
    assert "name: non_negative" in header and "name: accepted_values" in header
    assert "needs a numeric column" in _problems(_plan(checks=[{"column": "country", "check": "positive"}]))[0]
    assert "does not exist" in _problems(_plan(checks=[{"column": "nope", "check": "not_null"}]))[0]


def test_dedupe_without_order_warns_and_parse_date_is_explicit():
    c = _compile(_plan([{"op": "dedupe", "key": ["Order ID"]},
                        {"op": "parse_date", "column": "Order Date", "formats": ["%d/%m/%Y"]}]))
    assert any("no order_by" in w for w in c.warnings)
    assert "try_strptime(\"Order Date\", '%d/%m/%Y')" in _clean(c).body


def test_values_are_escaped_not_spliced():
    c = _compile(_plan([{"op": "replace_values", "column": "country",
                         "mapping": {"x'); DROP TABLE raw.orders; --": "y"}}]))
    assert "'x''); DROP TABLE raw.orders; --'" in _clean(c).body


# ─── outputs ──────────────────────────────────────────────────────────────────

def test_aggregate_split_and_raw_sql_outputs():
    c = _compile(_plan(
        [{"op": "cast", "columns": {"amount": "DOUBLE"}}],
        outputs=[{"kind": "aggregate", "name": "by_country", "source": "clean.orders", "group_by": ["country"],
                  "measures": [{"name": "n", "fn": "count"}, {"name": "revenue", "fn": "sum", "column": "amount"}]},
                 {"kind": "split", "name": "orders", "source": "clean.orders", "key": "Order ID",
                  "parts": {"train": 0.8, "test": 0.2}},
                 {"kind": "sql", "name": "top", "sources": ["mart.by_country"],
                  "query": "SELECT country, revenue FROM mart.by_country WHERE revenue > 100"}],
        exports=[{"asset": "mart.top"}]))
    names = [a.name for a in c.assets]
    assert names == ["raw.orders", "clean.orders", "mart.by_country", "mart.orders_train", "mart.orders_test", "mart.top"]
    top = next(a for a in c.assets if a.name == "mart.top")
    assert top.columns == [{"name": "country", "type": "VARCHAR"}, {"name": "revenue", "type": "DOUBLE"}]
    assert c.exports == [{"asset": "mart.top", "format": "csv"}]


def test_output_problems():
    problems = _problems(_plan(outputs=[
        {"kind": "aggregate", "name": "a", "source": "clean.orders", "measures": [{"name": "s", "fn": "sum", "column": "amount"}]},
        {"kind": "sql", "name": "b", "sources": ["clean.orders"], "query": "SELECT * FROM read_csv('/etc/passwd')"},
        {"kind": "sql", "name": "c", "sources": ["clean.nothing"], "query": "SELECT 1 AS x"},
        {"kind": "aggregate", "name": "d", "source": "clean.orders", "measures": [{"name": "n", "fn": "count"}],
         "group_by": ["region"]}],
        exports=[{"asset": "mart.missing"}]))
    text = "\n".join(problems)
    assert "sum needs BIGINT/INTEGER/DOUBLE" in text           # amount is still VARCHAR
    assert "table functions are not allowed" in text
    assert "not assets of this plan" in text
    assert "'region' does not exist" in text
    assert "export 'mart.missing'" in text


# ─── SQL the plan writes itself ───────────────────────────────────────────────

SCHEMA = {"order_id": "BIGINT", "amount": "DOUBLE", "country": "VARCHAR"}


@pytest.mark.parametrize("expr,why", [
    ("price * 2", "does not exist"), ("sum(amount)", "aggregate"), ("getenv('HOME')", "not allowed"),
    ("(SELECT 1)", "subqueries"), ("now()", "not allowed"), ("random()", "not allowed"),
])
def test_bad_expressions(expr, why):
    with pytest.raises(SqlCheckError, match=why):
        check_expression(expr, SCHEMA)


@pytest.mark.parametrize("query,why", [
    ("DELETE FROM clean.orders", "only a query"), ("SELECT 1 AS a; SELECT 2 AS b", "exactly one statement"),
    ("SELECT * FROM read_parquet('/x.parquet')", "table functions"), ("SELECT * FROM clean.secret", "not one of"),
    ("SELECT current_setting('home_directory') AS h", "not allowed"), ("SELECT price FROM clean.orders", "does not exist"),
    ("SELECT country, count(*) AS n, count(*) AS N FROM clean.orders GROUP BY country", "duplicate output"),
    ("COPY clean.orders TO '/tmp/x.csv'", "only a query"), ("ATTACH '/tmp/x.db'", "only a query"),
])
def test_bad_queries(query, why):
    with pytest.raises(SqlCheckError, match=why):
        check_query(query, {"clean.orders": SCHEMA})


def test_good_query_gets_materialized_ctes_and_typed_columns():
    sql, cols = check_query("WITH t AS (SELECT * FROM clean.orders) SELECT country, avg(amount) AS mean FROM t "
                            "GROUP BY country", {"clean.orders": SCHEMA})
    assert "AS MATERIALIZED" in sql and cols == [("country", "VARCHAR"), ("mean", "DOUBLE")]


# maths on a text column passed validation and failed only in `bruin run`
TEXT_COLS = {"s": "VARCHAR", "s2": "VARCHAR", "n": "INTEGER", "d": "DATE"}
TEXT_MATHS = [
    ("s * 2", False), ("(s) * 2", False), ("s + n", False), ("s + s2", False), ("-s", False),
    ("s % 2", False), ("trim(s) * 2", False), ("coalesce(s, 0) * 2", False), ("s > 10", False),
    ("1 < s", False), ("s <= n", False), ("s BETWEEN 1 AND 9", False), ("round(s, 2)", False),
    ("abs(s)", False),
    ("s = 1", True), ("s <> n", True), ("s IN (1, 2)", True), ("'5' * 2", True), ("CAST(s AS INT) * 2", True), ("TRY_CAST(s AS DOUBLE) * n", True), ("s = 'a'", True),
    ("s || 'x'", True), ("d - DATE '2026-01-01'", True), ("n * 2", True), ("-n", True),
    ("CASE WHEN s = 'a' THEN n * 2 ELSE 0 END", True),
]


@pytest.mark.parametrize("expr,ok", TEXT_MATHS)
def test_text_maths_is_refused_exactly_when_duckdb_would_fail(expr, ok):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE _src AS SELECT '5' AS s, '2' AS s2, 3 AS n, DATE '2026-01-02' AS d")
    try:
        con.execute(f"SELECT {expr} FROM _src").fetchall()
        runs = True
    except duckdb.Error:
        runs = False
    assert runs is ok                                                  # the table above is DuckDB's truth
    if ok:
        check_expression(expr, TEXT_COLS)
    else:
        with pytest.raises(SqlCheckError, match=r"is text \(VARCHAR\).*TRY_CAST"):
            check_expression(expr, TEXT_COLS)


def test_text_maths_in_a_query_and_in_a_plan_is_caught_at_validation():
    with pytest.raises(SqlCheckError, match=r"s2 is text \(VARCHAR\), so SUM\(s2\)"):
        check_query("SELECT s, sum(s2) AS t FROM clean.o GROUP BY s", {"clean.o": TEXT_COLS})
    check_query("SELECT s, avg(TRY_CAST(s2 AS DOUBLE)) AS t, min(s2) AS m FROM clean.o GROUP BY s",
                {"clean.o": TEXT_COLS})
    # raw columns are text until a cast step: amount * 2 is refused, and fine after the cast
    problems = _problems(_plan([{"op": "derive", "name": "double", "expression": "amount * 2"}]))
    assert any("amount is text (VARCHAR)" in p for p in problems), problems
    _compile(_plan([{"op": "cast", "columns": {"amount": "DOUBLE"}},
                    {"op": "derive", "name": "double", "expression": "amount * 2"}]))


# ─── saved plans ──────────────────────────────────────────────────────────────

def test_saved_plans_are_listed_and_fill_into_ordinary_plans():
    names = {p["name"] for p in list_saved()}
    assert {"invoice_cleanup", "ml_split"} <= names
    plan = instantiate("ml_split", {"dataset_id": "ds_0000000000000000", "id_column": "id",
                                    "target_column": "churned", "test_fraction": 0.2, "train_fraction": 0.8})
    parsed = parse_plan(plan)
    assert parsed.outputs[0].parts == {"train": 0.8, "test": 0.2}
    c = compile_plan(parsed, {"records": "/x.csv"}, {"records": ["id", "churned", "spend"]})
    assert [a.name for a in c.assets][-2:] == ["mart.records_train", "mart.records_test"]


def test_saved_plan_parameters_are_checked():
    with pytest.raises(SavedPlanError, match="missing parameters"):
        instantiate("ml_split", {"dataset_id": "x"})
    with pytest.raises(SavedPlanError, match="unknown parameters"):
        instantiate("ml_split", {"dataset_id": "x", "id_column": "i", "target_column": "t",
                                 "test_fraction": 0.2, "train_fraction": 0.8, "evil": 1})
    with pytest.raises(SavedPlanError, match="no saved plan"):
        instantiate("../etc/passwd", {})


def test_a_saved_plan_with_wrong_columns_fails_like_any_plan():
    plan = parse_plan(instantiate("invoice_cleanup", {
        "dataset_id": "ds_0000000000000000", "invoice_id_column": "inv", "customer_column": "cust",
        "currency_column": "cur", "amount_column": "amt", "date_column": "dt", "date_formats": ["%Y-%m-%d"]}))
    with pytest.raises(PlanError) as e:
        compile_plan(plan, {"invoices": "/x.csv"}, {"invoices": ["invoice_no", "customer", "amount"]})
    assert "'inv' does not exist" in e.value.problems[0]


# ─── usability: what live model runs got wrong (Phase 6, 2026-10-23) ──────────

def test_a_plan_sent_as_a_json_string_is_accepted():
    import json
    plan = parse_plan(json.dumps({"version": "1", "name": "t",
                                  "sources": [{"table": "orders", "dataset_id": "ds_0000000000000000"}]}))
    assert plan.version == 1
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_plan("{not json")


def test_column_and_columns_are_both_accepted_but_not_together():
    plan = _plan([{"op": "replace_values", "column": "country", "mapping": {"in": "IN"}},
                  {"op": "fill_null", "columns": ["customer", "country"], "value": "?"},
                  {"op": "parse_date", "column": "Order Date", "formats": ["%Y-%m-%d"]}])
    c = _compile(plan)
    assert "COALESCE(\"customer\", '?')" in _clean(c).body and "COALESCE(\"country\", '?')" in _clean(c).body
    with pytest.raises(ValidationError, match="not both"):
        _plan([{"op": "trim", "column": "a", "columns": ["b"]}])


def test_the_published_example_plan_is_valid_and_complete():
    from bruin_mcp_service.plan.model import EXAMPLE_PLAN, plan_format, step_fields
    fmt = plan_format()
    assert set(fmt["steps"]) == set(step_fields()) and len(fmt["steps"]) == 14
    used = {s["op"] for s in EXAMPLE_PLAN["clean"][0]["steps"]}
    assert used <= set(fmt["steps"])
    example = parse_plan({**EXAMPLE_PLAN, "sources": [{"table": "orders", "dataset_id": "ds_0000000000000000"}]})
    compile_plan(example, {"orders": "/x.csv"}, {"orders": ["Order ID", "customer", "country", "amount", "order_date"]})


def test_format_errors_say_what_a_step_accepts():
    from bruin_mcp_service.server import _format_errors
    with pytest.raises(ValidationError) as e:
        parse_plan({"name": "t", "sources": [{"table": "o", "dataset_id": "x"}],
                    "clean": [{"table": "o", "steps": [{"op": "dedupe", "options": {}}]}]})
    text = _format_errors(e.value)
    assert "dedupe accepts: op, key (optional), order_by (optional), keep (optional)" in text
    assert "get_plan_format" in text
