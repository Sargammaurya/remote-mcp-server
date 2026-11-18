"""The plan engine without Bruin: format, schema flow, SQL checks, saved plans."""

import pytest
from pydantic import ValidationError

from bruin_mcp_service.plan.engine import PlanError, compile_plan
from bruin_mcp_service.plan.model import parse_plan
from bruin_mcp_service.plan.rawsql import SqlCheckError, case_null_warnings, check_expression, check_query
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


# a live run labelled 23 people with no age 'Boomer+': NULL < 25 matches no WHEN
CASE_NULLS = [
    ("CASE WHEN Age < 25 THEN 'young' ELSE 'old' END", ["Age"]),
    ("CASE Age WHEN 1 THEN 'one' ELSE 'other' END", ["Age"]),
    ("CASE WHEN Age < 25 AND income > 9 THEN 'a' ELSE 'b' END", ["Age", "income"]),
    ("CASE WHEN Age IS NOT NULL AND Age < 25 THEN 'young' ELSE 'old' END", ["Age"]),
    ("coalesce(CASE WHEN Age < 25 THEN 'young' ELSE 'old' END, 'x')", ["Age"]),
    ("CASE WHEN Age IS NULL THEN NULL WHEN Age < 25 THEN 'young' ELSE 'old' END", []),
    ("CASE WHEN coalesce(Age, 0) < 25 THEN 'young' ELSE 'old' END", []),
    ("CASE WHEN Age < 25 THEN 'young' END", []),
    ("CASE WHEN Age < 25 THEN 'young' ELSE NULL END", []),
]


@pytest.mark.parametrize("expr,exposed", CASE_NULLS)
def test_case_else_that_catches_missing_values_is_warned_about(expr, exposed):
    warnings = case_null_warnings(expr)
    if not exposed:
        assert warnings == []
        return
    assert len(warnings) == 1 and f"rows where {', '.join(exposed)} is NULL" in warnings[0]
    assert f"WHEN {exposed[0]} IS NULL THEN NULL" in warnings[0]
    duckdb = pytest.importorskip("duckdb")                       # and DuckDB really does send NULL to ELSE
    got = duckdb.connect().execute(f"SELECT {expr} FROM (SELECT NULL::INT AS Age, 10 AS income)").fetchone()[0]
    assert got is not None and got in ("old", "other", "b")


def test_case_null_warnings_reach_the_plan_from_derive_and_sql_outputs():
    plan = _plan([{"op": "cast", "columns": {"amount": "DOUBLE"}},
                  {"op": "derive", "name": "size", "expression": "CASE WHEN amount < 100 THEN 'small' ELSE 'big' END"}],
                 outputs=[{"kind": "sql", "name": "tiers", "sources": ["clean.orders"],
                           "query": "SELECT CASE WHEN country = 'IN' THEN 'home' ELSE 'abroad' END AS tier "
                                    "FROM clean.orders"}])
    warnings = _compile(plan).warnings
    assert any("derive 'size': rows where amount is NULL" in w and "'big'" in w for w in warnings), warnings
    assert any(w.startswith("mart.tiers: rows where country is NULL") for w in warnings), warnings


def test_an_output_of_a_failed_table_says_so_instead_of_not_an_asset():
    # live run: a bad derive failed clean.ai_data, and its aggregate then said
    # "source 'clean.ai_data' is not an asset of this plan" — read like a typo
    plan = _plan([{"op": "derive", "name": "double", "expression": "amount * 2"}],
                 outputs=[{"kind": "aggregate", "name": "by_country", "source": "clean.orders",
                           "group_by": ["country"], "measures": [{"name": "n", "fn": "count"}]},
                          {"kind": "sql", "name": "q", "sources": ["clean.orders", "clean.typo"],
                           "query": "SELECT 1 AS one FROM clean.orders"},
                          {"kind": "aggregate", "name": "of_mart", "source": "mart.by_country",
                           "group_by": ["country"], "measures": [{"name": "n", "fn": "count"}]}])
    problems = _problems(plan)
    assert any("amount is text (VARCHAR)" in p for p in problems)                         # the real cause
    assert "mart.by_country: source 'clean.orders' was not built because of the problem(s) reported " \
           "above for it" in problems
    assert any(p.startswith("mart.q: sources ['clean.orders'] were not built") for p in problems), problems
    assert any(p.startswith("mart.q: sources ['clean.typo'] are not assets of this plan") for p in problems)
    assert "mart.of_mart: source 'mart.by_country' was not built because of the problem(s) reported " \
           "above for it" in problems
    assert not any("'clean.orders' is not an asset" in p for p in problems)


def test_an_export_of_a_failed_table_says_so_instead_of_did_you_mean_the_raw_one():
    # live run: a check on a text column failed clean.hyper, and its export then
    # said "did you mean 'raw.hyper' for 'clean.hyper'?" — the uncleaned table
    plan = _plan([{"op": "derive", "name": "double", "expression": "amount * 2"}],
                 outputs=[{"kind": "aggregate", "name": "by_country", "source": "clean.orders",
                           "group_by": ["country"], "measures": [{"name": "n", "fn": "count"}]}],
                 exports=[{"asset": "clean.orders"}, {"asset": "mart.by_country"}, {"asset": "by_country"}])
    problems = _problems(plan)
    assert "export 'clean.orders' was not built because of the problem(s) reported above for it" in problems
    assert "export 'mart.by_country' was not built because of the problem(s) reported above for it" in problems
    assert any(p.startswith("export 'by_country' is not an asset of this plan — did you mean 'mart.by_country'")
               for p in problems), problems
    assert not any("did you mean 'raw.orders'" in p for p in problems), problems


@pytest.mark.parametrize("expression,warned", [
    ("dayofweek(d)", True), ("extract(dow FROM d)", True), ("dayofweek(d) IN (0, 6)", True),
    ("isodow(d)", False), ("month(d)", False), ("extract(year FROM d)", False),
])
def test_a_day_of_week_derive_says_which_day_is_zero(expression, warned):
    c = _compile(_plan([{"op": "parse_date", "columns": ["Order Date"], "formats": ["%Y-%m-%d"]},
                        {"op": "rename", "mapping": {"Order Date": "d"}},
                        {"op": "derive", "name": "wd", "expression": expression, "type": "INTEGER"}]))
    assert any("derive 'wd': dayofweek counts Sunday = 0" in w for w in c.warnings) is warned, c.warnings


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


# a live run: "remove cancelled orders" became status ne 'cancelled', and plain SQL <>
# also dropped the 12 orders whose status was blank — 36 rows left instead of 48
FILTER_NULLS = [
    ({"column": "status", "operator": "ne", "value": "cancelled"}, [1, 3, 4]),
    ({"column": "status", "operator": "not_in", "value": ["cancelled", "done"]}, [4]),
    ({"column": "amount", "operator": "ne", "value": 5}, [1, 2, 3, 4]),
    ({"all": [{"column": "status", "operator": "ne", "value": "cancelled"},
              {"column": "status", "operator": "not_null"}]}, [1, 3]),
    ({"column": "status", "operator": "eq", "value": "done"}, [1, 3]),
    ({"column": "status", "operator": "in", "value": ["done", "cancelled"]}, [1, 2, 3]),
    ({"column": "amount", "operator": "gt", "value": 9}, [2, 3]),
]


@pytest.mark.parametrize("where,kept", FILTER_NULLS)
def test_ne_and_not_in_keep_rows_whose_value_is_missing(tmp_path, where, kept):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "orders.csv"
    path.write_text("id,status,amount\n1,done,\n2,cancelled,10\n3,done,20\n4,,3\n")
    plan = _plan([{"op": "cast", "columns": {"id": "INTEGER", "amount": "INTEGER"}}, {"op": "filter", "where": where}])
    c = compile_plan(plan, {"orders": str(path)}, {"orders": ["id", "status", "amount"]})
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw")
    raw = next(a for a in c.assets if a.name == "raw.orders")
    con.execute(f"CREATE TABLE raw.orders AS {raw.body}")
    got = sorted(r[0] for r in con.execute(_clean(c).body).fetchall())
    assert got == kept


def test_an_unknown_source_names_its_likely_meaning_before_the_long_list():
    # live run: an aggregate read 'clean.orders_customers' (the join output is
    # mart.orders_customers); the client's view of the error was cut inside the known
    # list, so it took two more tries
    join = {"kind": "sql", "name": "orders_customers", "sources": ["clean.orders"],
            "query": "SELECT country, amount FROM clean.orders"}
    agg = {"kind": "aggregate", "name": "spend", "source": "clean.orders_customers",
           "group_by": ["country"], "measures": [{"name": "n", "fn": "count"}]}
    problem = next(p for p in _problems(_plan(outputs=[join, agg])) if p.startswith("mart.spend"))
    assert "did you mean 'mart.orders_customers' for 'clean.orders_customers'?" in problem
    assert problem.index("did you mean") < problem.index("(known:")
    later = next(p for p in _problems(_plan(outputs=[{**agg, "source": "mart.orders_customers"}, join]))
                 if p.startswith("mart.spend"))
    assert "'mart.orders_customers' (for 'mart.orders_customers'?) is defined later in outputs" in later
    itself = next(p for p in _problems(_plan(outputs=[{**agg, "source": "mart.spend"}])) if p.startswith("mart.spend"))
    assert "defined later" not in itself
    nothing = next(p for p in _problems(_plan(outputs=[{**agg, "source": "zzz"}])) if p.startswith("mart.spend"))
    assert "'zzz' is not an asset of this plan (known:" in nothing and "did you mean" not in nothing


@pytest.mark.parametrize("steps,expected", [
    ([{"op": "cast", "columns": {"amount": "INTEGER"}}], [None, 3, None, None, 7, 1000]),
    ([{"op": "cast", "columns": {"amount": "BIGINT"}}], [None, 3, None, None, 7, 1000]),
    ([{"op": "cast", "columns": {"amount": "DOUBLE"}}, {"op": "cast", "columns": {"amount": "INTEGER"}}],
     [None, 3, None, None, 7, 1000]),
    ([{"op": "cast", "columns": {"amount": "DOUBLE"}}], [3.5, 3.0, None, -2.5, 7.0, 1000.0]),
])
def test_an_integer_cast_makes_fractions_null_instead_of_rounding_them(tmp_path, steps, expected):
    # TRY_CAST('3.5' AS INTEGER) is 4 and '-2.5' is -3: the data changed with no sign of it
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "orders.csv"
    path.write_text("id,amount\n1,3.5\n2,3.0\n3,abc\n4,-2.5\n5, 7 \n6,1e3\n")
    c = compile_plan(_plan([{"op": "cast", "columns": {"id": "INTEGER"}}, *steps]),
                     {"orders": str(path)}, {"orders": ["id", "amount"]})
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw")
    con.execute(f"CREATE TABLE raw.orders AS {next(a for a in c.assets if a.name == 'raw.orders').body}")
    assert [r[1] for r in con.execute(_clean(c).body + " ORDER BY id").fetchall()] == expected
    integer = steps[-1]["columns"]["amount"] in ("INTEGER", "BIGINT")
    assert any("['amount'] (to a whole-number type)" in w and "not rounded" in w for w in c.warnings) is integer


@pytest.mark.parametrize("a,b,like", [
    ("undergrad", "Undergraduate", True), ("Bachelors", "Bachelor's", True), ("U.K.", "uk", True),
    ("Mastres", "Masters", True), ("Male", "female", False), ("Eco", "Eco Plus", False),
    ("Diploma", "Undergraduate", False), ("Nigeria", "india", False), ("", "x", False),
    ("Loyal Customer", "disloyal Customer", False), ("known", "Unknown", False), ("Femal", "Female", True),
])
def test_looks_like_catches_spelling_variants_not_different_categories(a, b, like):
    from bruin_mcp_service.plan.engine import looks_like
    assert looks_like(a, b) is like


def test_the_trace_records_what_each_value_step_rewrites_and_where_columns_came_from():
    c = _compile(_plan([{"op": "rename", "mapping": {"Order ID": "order_id", "country": "nation"}},
                        {"op": "trim", "columns": ["customer"]},
                        {"op": "derive", "name": "x", "expression": "1", "type": "INTEGER"},
                        {"op": "drop", "columns": ["amount"]}, {"op": "dedupe"},
                        {"op": "rename", "mapping": {"nation": "cntry"}}]))
    clean = _clean(c)
    assert clean.origins == {"order_id": "Order ID", "customer": "customer", "cntry": "country",
                             "Order Date": "Order Date", "x": None}
    changed = {t["step"]: t.get("changed") for t in clean.trace}
    assert changed[2] == {"customer": 'TRIM("customer")'}
    assert all(changed[i] is None for i in (0, 1, 3, 4, 5, 6))         # renames, derive, drop, dedupe rewrite nothing
