"""
Compiled plans against the real pinned Bruin: write → `bruin validate` →
EXECUTE with `bruin run` in a temporary workspace → read the tables back and
check the values. Static validation is not proof the SQL works (DuckDB 1.5.2
crashed on one CTE shape that validated fine), so every op here is run.

SYNTHETIC data only (tests/synthetic.py).
"""

import json
import os

import pytest

from bruin_mcp_service import datasets
from bruin_mcp_service.bruin_cli import run_bruin
from bruin_mcp_service.config import load_settings
from bruin_mcp_service.plan import pipeline as plans
from bruin_mcp_service.plan.engine import PlanError
from bruin_mcp_service.plan.saved import instantiate
from conftest import needs_bruin
from synthetic import MESSY_ORDERS_CSV, b64

pytestmark = needs_bruin


def _build(settings, user, plan_data):
    """Parse then build — what compile_plan does with a stored draft."""
    from bruin_mcp_service.plan.model import parse_plan
    return plans.build(settings, user, parse_plan(plan_data))

INVOICES_CSV = """invoice_no,client,ccy,total,issued
INV-1, Acme ,inr,"1,200.50",2026-01-05
INV-2,Globex,usd,$980,06/01/2026
INV-2,Globex,usd,$990,07/01/2026
INV-3, Acme ,INR,300,2026-01-09
"""


@pytest.fixture
def settings(real_env):
    return load_settings(real_env)


@pytest.fixture
def orders(settings):
    return datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]


def _root(settings, record, user="alice"):
    return settings.workspace_root / "users" / user / "pipelines" / record["pipeline_id"]


def _run(settings, root):
    r = run_bruin(settings, ["run", "--config-file", str(root / ".bruin.yml"), "--no-color", "--no-timestamp",
                             str(root / "pipeline")], cwd=root, timeout=300)
    return r.returncode, r.stdout + r.stderr


def _rows(settings, root, sql):
    r = run_bruin(settings, ["query", "--config-file", str(root / ".bruin.yml"), "-c", "duckdb-default",
                             "-o", "json", "-q", sql], cwd=root, timeout=120)
    payload = json.loads(r.stdout)
    assert "error" not in payload, payload
    names = [c["name"] for c in payload["columns"]]
    return [dict(zip(names, row)) for row in payload["rows"]]


def _orders_plan(dataset_id, checks=None, outputs=None):
    return {
        "name": "orders_prep",
        "sources": [{"table": "orders", "dataset_id": dataset_id}],
        "clean": [{"table": "orders", "steps": [
            {"op": "trim", "columns": ["customer", "country", "amount"]},
            {"op": "change_case", "columns": ["country"], "to": "upper"},
            {"op": "null_if", "columns": ["customer", "country", "amount"], "values": [""]},
            {"op": "cast", "columns": {"order_id": "BIGINT", "amount": "DOUBLE"}},
            {"op": "parse_date", "column": "order_date", "formats": ["%Y-%m-%d", "%d/%m/%Y"]},
            {"op": "dedupe", "key": ["order_id"], "order_by": ["order_date"], "keep": "last"},
        ], "checks": checks if checks is not None else [
            {"column": "order_id", "check": "not_null"}, {"column": "order_id", "check": "unique"}]}],
        "outputs": outputs or [],
    }


def test_build_writes_one_validated_cleaning_asset_and_nothing_runs(settings, orders):
    record = _build(settings, "alice", _orders_plan(orders))
    root = _root(settings, record)
    assert record["bruin_validate"] == "passed"
    assert sorted(record["assets"]) == ["clean.orders", "raw.orders"]
    assert (root / "pipeline/assets/clean/orders.sql").is_file() and (root / "plan.json").is_file()
    assert not (root / "warehouse.duckdb").exists()                 # compiled, not executed
    rendered = record["assets"]["clean.orders"]["rendered"]
    assert "CREATE TABLE clean.orders AS WITH" in rendered and rendered.count("AS MATERIALIZED") == 7
    assert not list((settings.workspace_root / "users/alice/pipelines").glob(".staging-*"))


def test_the_compiled_cleaning_runs_and_produces_the_right_values(settings, orders):
    record = _build(settings, "alice", _orders_plan(orders))
    root = _root(settings, record)
    code, log = _run(settings, root)
    assert code == 0 and "bruin run completed successfully" in log, log[-1500:]
    rows = {r["order_id"]: r for r in _rows(settings, root, "SELECT * FROM clean.orders ORDER BY order_id")}
    assert sorted(rows) == list(range(1, 11))                         # 12 rows, 2 exact duplicates gone
    assert rows[1] == {"order_id": 1, "customer": "Alice", "amount": 100.5, "order_date": "2026-01-05",
                       "country": "IN"} or rows[1]["order_date"].startswith("2026-01-05")
    assert rows[2]["country"] == "IN" and rows[2]["amount"] is None   # 'in' → IN; '' → NULL
    assert rows[3]["order_date"].startswith("2026-01-05")             # 05/01/2026 read day-first, as stated
    assert rows[4]["amount"] is None and rows[4]["order_date"] is None and rows[4]["customer"] is None
    assert rows[6]["amount"] == 300.0                                 # '  300 ' trimmed and cast
    assert rows[7]["amount"] == 1000.0 and rows[7]["country"] == "UK" # '1e3', 'uk'
    assert rows[9]["order_date"] is None                              # 2026-13-01 is not a date
    types = _rows(settings, root, "SELECT column_name, data_type FROM information_schema.columns "
                                  "WHERE table_schema = 'clean' AND table_name = 'orders' ORDER BY ordinal_position")
    assert [(t["column_name"], t["data_type"]) for t in types] == [
        ("order_id", "BIGINT"), ("customer", "VARCHAR"), ("amount", "DOUBLE"),
        ("order_date", "DATE"), ("country", "VARCHAR")]


def test_a_failing_quality_check_fails_the_run(settings, orders):
    plan = _orders_plan(orders, checks=[{"column": "country", "check": "accepted_values", "value": ["IN", "US"]}])
    root = _root(settings, _build(settings, "alice", plan))
    code, log = _run(settings, root)
    assert code != 0 and "accepted_values" in log


def test_every_other_op_and_output_kind_runs(settings, orders):
    plan = {
        "name": "all_ops",
        "sources": [{"table": "orders", "dataset_id": orders}],
        "clean": [{"table": "orders", "steps": [
            {"op": "rename", "mapping": {"customer": "client"}},
            {"op": "trim", "columns": ["client", "country", "amount"]},
            {"op": "change_case", "columns": ["client"], "to": "lower"},
            {"op": "replace_values", "column": "country", "mapping": {"in": "IN", "uk": "UK"}},
            {"op": "null_if", "columns": ["amount"], "values": ["", "abc"]},
            {"op": "strip_chars", "columns": ["client"], "chars": ","},
            {"op": "cast", "columns": {"order_id": "BIGINT", "amount": "DOUBLE"}},
            {"op": "fill_null", "column": "amount", "value": 0.0},
            {"op": "fill_null", "column": "country", "value": "UNKNOWN"},
            {"op": "derive", "name": "big", "expression": "amount >= 100"},
            {"op": "dedupe"},
            {"op": "filter", "where": {"any": [{"column": "amount", "operator": "ge", "value": 0},
                                               {"column": "country", "operator": "in", "value": ["UNKNOWN"]}]}},
            {"op": "drop", "columns": ["order_date"]},
            {"op": "select", "columns": ["order_id", "client", "country", "amount", "big"]},
        ]}],
        "outputs": [
            {"kind": "aggregate", "name": "by_country", "source": "clean.orders", "group_by": ["country"],
             "measures": [{"name": "n", "fn": "count"}, {"name": "revenue", "fn": "sum", "column": "amount"},
                          {"name": "clients", "fn": "count_distinct", "column": "client"}],
             "checks": [{"column": "n", "check": "positive"}]},
            {"kind": "split", "name": "orders", "source": "clean.orders", "key": "order_id",
             "parts": {"train": 0.7, "test": 0.3}},
            {"kind": "sql", "name": "top_country", "sources": ["mart.by_country"],
             "query": "WITH r AS (SELECT country, revenue FROM mart.by_country) "
                      "SELECT country, revenue FROM r QUALIFY rank() OVER (ORDER BY revenue DESC) = 1"},
        ],
    }
    root = _root(settings, _build(settings, "alice", plan))
    code, log = _run(settings, root)
    assert code == 0, log[-2000:]
    clean = _rows(settings, root, "SELECT * FROM clean.orders ORDER BY order_id")
    # exact duplicates of 3 and 5 removed; order 8 (amount -5, country IN) fails the filter
    assert [r["order_id"] for r in clean] == [1, 2, 3, 4, 5, 6, 7, 9, 10]
    assert next(r for r in clean if r["order_id"] == 3)["client"] == "carol ltd"
    assert next(r for r in clean if r["order_id"] == 4)["amount"] == 0.0
    assert next(r for r in clean if r["order_id"] == 10)["country"] == "UNKNOWN"
    assert next(r for r in clean if r["order_id"] == 1)["big"] is True
    totals = {r["country"]: r for r in _rows(settings, root, "SELECT * FROM mart.by_country")}
    assert sum(r["n"] for r in totals.values()) == len(clean)
    train = {r["order_id"] for r in _rows(settings, root, "SELECT order_id FROM mart.orders_train")}
    test = {r["order_id"] for r in _rows(settings, root, "SELECT order_id FROM mart.orders_test")}
    assert not train & test and train | test == {r["order_id"] for r in clean}
    top = _rows(settings, root, "SELECT * FROM mart.top_country")
    assert top[0]["revenue"] == max(r["revenue"] for r in totals.values())


def test_invented_columns_fail_before_anything_is_written(settings, orders):
    plan = _orders_plan(orders)
    plan["clean"][0]["steps"].append({"op": "derive", "name": "x", "expression": "discount * 2"})
    with pytest.raises(PlanError) as e:
        _build(settings, "alice", plan)
    assert "'discount'" in e.value.problems[0]
    root = settings.workspace_root / "users/alice/pipelines"
    assert not [p for p in root.iterdir()] if root.exists() else True


def test_another_users_dataset_cannot_be_a_source(settings, orders):
    with pytest.raises(PlanError, match="no dataset"):
        _build(settings, "bob", _orders_plan(orders))


def test_pipelines_are_listed_read_and_deleted_per_user(settings, orders):
    record = _build(settings, "alice", _orders_plan(orders))
    pid = record["pipeline_id"]
    assert [p["pipeline_id"] for p in plans.list_all(settings, "alice")] == [pid]
    assert plans.list_all(settings, "bob") == []
    with pytest.raises(plans.PipelineError):
        plans.get(settings, "bob", pid)
    assert plans.get(settings, "alice", pid)["pipeline_id"] == pid
    plans.delete(settings, "alice", pid)
    assert plans.list_all(settings, "alice") == []


def test_the_saved_invoice_plan_runs_through_the_same_engine(settings):
    ds = datasets.upload(settings, "alice", "synthetic_invoices.csv", b64(INVOICES_CSV))["dataset_id"]
    plan = instantiate("invoice_cleanup", {
        "dataset_id": ds, "invoice_id_column": "invoice_no", "customer_column": "client",
        "currency_column": "ccy", "amount_column": "total", "date_column": "issued",
        "date_formats": ["%Y-%m-%d", "%d/%m/%Y"]})
    root = _root(settings, _build(settings, "alice", plan))
    code, log = _run(settings, root)
    assert code == 0, log[-1500:]
    totals = {(r["client"], r["ccy"]): r for r in _rows(settings, root, "SELECT * FROM mart.invoice_totals")}
    assert totals[("Acme", "INR")]["total_amount"] == 1500.5 and totals[("Acme", "INR")]["invoices"] == 2
    assert totals[("Globex", "USD")]["total_amount"] == 990.0                 # latest INV-2 kept
