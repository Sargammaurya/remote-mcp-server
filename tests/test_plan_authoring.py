"""
Plan authoring (Phase 6 follow-up, item 1).

Live runs showed the model hand-writing plans as JSON strings with mismatched
brackets (7 of 9 attempts). The fixes tested here:
  * the full plan schema is published in the tool definitions (generated from
    the same models that validate, all $refs inlined, unknown fields forbidden);
  * a flat single-table form (validate_table_plan) with two fewer nesting levels;
  * validate stores a draft and compile builds exactly that draft, echoing every
    step received — so nothing requested can be dropped in between;
  * nothing is ever repaired: malformed input is refused and nothing is stored.
"""

import json
import os

import jsonschema
import pytest
from pydantic import ValidationError

from bruin_mcp_service import datasets
from bruin_mcp_service.config import load_settings
from bruin_mcp_service.plan import model as m
from bruin_mcp_service.plan import pipeline as plans
from bruin_mcp_service.plan.engine import compile_plan
from bruin_mcp_service.plan.schema import plan_tool_schema, table_plan_tool_schema
from conftest import needs_bruin
from synthetic import MESSY_ORDERS_CSV, b64

COLS = ["order_id", "customer", "amount", "order_date", "country"]
STEPS = [
    {"op": "trim", "columns": ["customer", "country", "amount"]},
    {"op": "change_case", "columns": ["country"], "to": "upper"},
    {"op": "replace_values", "columns": ["country"], "mapping": {"USA": "US"}},
    {"op": "null_if", "columns": ["amount"], "values": ["", "n/a"]},
    {"op": "cast", "columns": {"order_id": "BIGINT", "amount": "DOUBLE"}},
    {"op": "parse_date", "columns": ["order_date"], "formats": ["%Y-%m-%d", "%d/%m/%Y"]},
    {"op": "dedupe", "key": ["order_id"], "order_by": ["order_date"], "keep": "last"},
]
FLAT = {"dataset_id": "ds_0000000000000000", "table": "orders", "steps": STEPS,
        "checks": [{"column": "order_id", "check": "unique"}],
        "outputs": [{"kind": "aggregate", "name": "by_country", "source": "clean.orders",
                     "group_by": ["country"], "measures": [{"name": "n", "fn": "count"}]}]}


# ─── the published schemas ────────────────────────────────────────────────────

def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@pytest.mark.parametrize("schema_fn", [plan_tool_schema, table_plan_tool_schema])
def test_published_schemas_are_self_contained_and_strict(schema_fn):
    schema = schema_fn()
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and "discriminator" not in text
    jsonschema.Draft202012Validator.check_schema(schema)
    objects = [n for n in _walk(schema) if isinstance(n, dict) and n.get("type") == "object" and "properties" in n]
    assert objects and all(o.get("additionalProperties") is False for o in objects)


def test_the_flat_schema_lists_every_step_with_its_fields():
    steps = table_plan_tool_schema()["properties"]["steps"]["items"]["oneOf"]
    by_op = {v["properties"]["op"]["const"]: v for v in steps}
    assert set(by_op) == set(m.step_fields())
    for op, fields in m.step_fields().items():
        assert set(by_op[op]["properties"]) - {"op"} == set(fields), op
        assert by_op[op].get("description"), f"{op} has no description for the model"


def test_schema_and_models_agree_on_good_and_bad_plans():
    flat_validator = jsonschema.Draft202012Validator(table_plan_tool_schema())
    flat_validator.validate(FLAT)                                         # schema accepts it …
    m.parse_table_plan(FLAT)                                              # … and so does the model
    full_validator = jsonschema.Draft202012Validator(plan_tool_schema())
    full_validator.validate({"plan": m.EXAMPLE_PLAN})
    bad_cases = [
        {**FLAT, "colour": "red"},                                                      # unknown top-level field
        {**FLAT, "steps": [{"op": "dedupe", "options": {}}]},                           # unknown step field
        {**FLAT, "steps": [{"op": "explode", "columns": ["a"]}]},                       # unknown op
        {**FLAT, "steps": [{"op": "change_case", "columns": ["a"], "to": "title"}]},    # bad enum
        {**FLAT, "steps": [{"op": "cast", "columns": {"a": "MONEY"}}]},                 # bad type
        {k: v for k, v in FLAT.items() if k != "dataset_id"},                          # missing required
    ]
    for bad in bad_cases:
        assert list(flat_validator.iter_errors(bad)), bad
        with pytest.raises(ValidationError):
            m.parse_table_plan(bad)


# ─── flat form = full plan ────────────────────────────────────────────────────

def test_the_flat_form_compiles_to_exactly_the_same_sql_as_the_full_plan():
    flat = m.parse_table_plan(FLAT)
    full = m.parse_plan({"name": "orders_prep", "sources": [{"table": "orders", "dataset_id": FLAT["dataset_id"]}],
                         "clean": [{"table": "orders", "steps": STEPS, "checks": FLAT["checks"]}],
                         "outputs": FLAT["outputs"]})
    a = compile_plan(flat, {"orders": "/x.csv"}, {"orders": COLS})
    b = compile_plan(full, {"orders": "/x.csv"}, {"orders": COLS})
    assert [x.text() for x in a.assets] == [x.text() for x in b.assets]


def test_the_flat_form_defaults_and_rejects_bad_table_names():
    plan = m.parse_table_plan({"dataset_id": "ds_0000000000000000", "steps": [{"op": "trim", "columns": ["a"]}]})
    assert plan.name == "data_prep" and plan.sources[0].table == "data"
    with pytest.raises(ValidationError):
        m.parse_table_plan({"dataset_id": "x", "table": "Order Table"})


# ─── malformed input is refused, never repaired ───────────────────────────────

@pytest.mark.parametrize("text", [
    '{"version": 1, "name": "t", "sources": [{"table": "o", "dataset_id": "x"}]}]',   # extra closing bracket
    '{"version": 1, "name": "t", "sources": [{"table": "o", "dataset_id": "x"},]}',   # stray comma
    "{'version': 1, 'name': 't'}",                                                    # python-style quotes
    '{"version": 1, "name": "t", "sources": [{"table": "o", "dataset_id": "x"}',      # truncated
])
def test_malformed_json_strings_are_refused_not_repaired(text):
    with pytest.raises(ValueError, match="not valid JSON"):
        m.parse_plan(text)


# ─── ambiguity is surfaced, not guessed ───────────────────────────────────────

def test_ambiguous_requests_are_not_guessed():
    # no date format given: refused (the plan must state the day/month order)
    with pytest.raises(ValidationError):
        m.parse_table_plan({**FLAT, "steps": [{"op": "parse_date", "columns": ["order_date"], "formats": []}]})
    # dedupe with a key but no order: accepted, with a warning that the kept row is undefined
    plan = m.parse_table_plan({**FLAT, "steps": [{"op": "dedupe", "key": ["order_id"]}], "outputs": []})
    out = compile_plan(plan, {"orders": "/x.csv"}, {"orders": COLS})
    assert any("not defined" in w for w in out.warnings)
    # a split must give its fractions; nothing defaults to 80/20 silently
    with pytest.raises(ValidationError):
        m.parse_table_plan({**FLAT, "outputs": [{"kind": "split", "name": "s", "source": "clean.orders",
                                                 "key": "order_id", "parts": {}}]})


# ─── every requested step survives validate → draft → compile ────────────────

@pytest.fixture
def settings(real_env):
    return load_settings(real_env)


@needs_bruin
def test_steps_received_lists_every_step_and_compile_builds_exactly_the_draft(settings):
    ds = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]
    flat = {**FLAT, "dataset_id": ds}
    report = plans.validate(settings, "alice", m.parse_table_plan(flat))
    received = report["steps_received"]["clean.orders"]
    assert len(received) == len(STEPS)
    assert [line.split(". ", 1)[1].split(" ", 1)[0] for line in received] == [s["op"] for s in STEPS]
    assert 'mapping={"USA": "US"}' in received[2] and '"%d/%m/%Y"' in received[5]
    assert [t["op"] for t in report["assets"]["clean.orders"]["steps"]][1:] == [s["op"] for s in STEPS]

    draft = plans.load_draft(settings, "alice", report["draft_id"])
    record = plans.build(settings, "alice", draft)
    assert record["steps_received"] == report["steps_received"]
    sql = record["assets"]["clean.orders"]["sql"]
    for needle in ("TRIM(", "UPPER(", "'USA' THEN 'US'", "TRY_CAST", "try_strptime", "QUALIFY"):
        assert needle in sql, needle


@needs_bruin
def test_drafts_are_private_and_expire(settings, monkeypatch):
    ds = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]
    draft_id = plans.validate(settings, "alice", m.parse_table_plan({**FLAT, "dataset_id": ds}))["draft_id"]
    with pytest.raises(plans.PipelineError, match="no draft"):
        plans.load_draft(settings, "bob", draft_id)
    with pytest.raises(plans.PipelineError, match="no draft"):
        plans.load_draft(settings, "alice", "../alice/drafts/x")
    monkeypatch.setattr(plans, "DRAFT_TTL_S", -1)
    with pytest.raises(plans.PipelineError, match="expired"):
        plans.load_draft(settings, "alice", draft_id)


@needs_bruin
def test_a_rejected_plan_stores_no_draft_and_no_pipeline(settings):
    ds = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]
    bad = {**FLAT, "dataset_id": ds, "steps": STEPS + [{"op": "trim", "columns": ["discount"]}]}
    with pytest.raises(Exception, match="discount"):
        plans.validate(settings, "alice", m.parse_table_plan(bad))
    user = settings.workspace_root / "users" / "alice"
    assert not list((user / "drafts").glob("*.json")) if (user / "drafts").exists() else True
    assert not (user / "pipelines").exists() or not os.listdir(user / "pipelines")


def test_the_compile_guard_refuses_a_dropped_step():
    plan = m.parse_table_plan(FLAT)
    compiled = compile_plan(plan, {"orders": "/x.csv"}, {"orders": COLS})
    clean = next(a for a in compiled.assets if a.name == "clean.orders")
    clean.trace.pop(3)                                                    # simulate a lost step
    with pytest.raises(AssertionError, match="differ from the requested"):
        plans._check_every_step_compiled(plan, compiled)


def test_a_draft_id_given_as_a_pipeline_id_says_what_to_do(tmp_path, base_env):
    from bruin_mcp_service.config import load_settings as _load
    with pytest.raises(plans.PipelineError, match=r"is a draft_id.*compile_plan"):
        plans.get(_load(base_env), "alice", "dr_0123456789abcdef")


def test_a_mapping_key_an_earlier_step_made_impossible_is_reported():
    """Live L2 round 5: upper-cased in step 2, then {"India": "IN"} in step 3 matched nothing."""
    plan = m.parse_table_plan({**FLAT, "outputs": [], "steps": [
        {"op": "change_case", "columns": ["country"], "to": "upper"},
        {"op": "replace_values", "columns": ["country"], "mapping": {"India": "IN", "USA": "US"}}]})
    out = compile_plan(plan, {"orders": "/x.csv"}, {"orders": COLS})
    hits = [w for w in out.warnings if "can never match" in w]
    assert len(hits) == 1 and "'India'" in hits[0] and "INDIA" in hits[0] and "USA" not in hits[0].split("—")[0]
