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
from bruin_mcp_service.plan.engine import PlanError, compile_plan
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
def test_mapping_keys_that_match_nothing_at_their_step_are_reported(settings, monkeypatch):
    # live run: 'disloyal customer' was mapped where the data said 'disloyal Customer';
    # it validated, ran, replaced nothing, and the summary said it had been replaced
    ds = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]
    steps = [{"op": "trim", "columns": ["country"]},
             {"op": "replace_values", "columns": ["country"], "mapping": {"uk": "UK", "Uk": "UK", "USA": "US"}},
             {"op": "change_case", "columns": ["country"], "to": "upper"},
             {"op": "replace_values", "columns": ["country"], "mapping": {"IN": "India", "in": "India"}}]
    plan = m.parse_table_plan({"dataset_id": ds, "table": "orders", "steps": steps})
    warnings = [w for w in plans.validate(settings, "alice", plan)["warnings"] if "match no value" in w]
    assert warnings == [
        "clean.orders step 2 (replace_values): mapping key(s) 'USA', 'Uk' (did you mean 'uk'?) match no value "
        "of 'country' in the data at this step, so they will replace nothing",
        "clean.orders step 4 (replace_values): mapping key(s) 'in' (did you mean 'IN'?) match no value "
        "of 'country' in the data at this step, so they will replace nothing"], warnings

    every_key_matches = m.parse_table_plan({"dataset_id": ds, "table": "orders", "steps": [
        {"op": "replace_values", "columns": ["country"], "mapping": {"uk": "UK", "in": "IN"}}]})
    assert not [w for w in plans.validate(settings, "alice", every_key_matches)["warnings"] if "replace" in w]

    monkeypatch.setattr(plans, "MAPPING_CHECK_TIMEOUT_S", 0.001)     # too slow to check: say so, still valid
    report = plans.validate(settings, "alice", plan)
    assert report["valid"] and any("keys were not checked against the data" in w for w in report["warnings"])


def test_a_broken_json_string_for_steps_gets_a_precise_error_not_valid_list(base_env):
    # live run: steps arrived as a JSON string that had lost its closing ']'; FastMCP's
    # own JSON pre-parse skipped it and pydantic said only "Input should be a valid list"
    from bruin_mcp_service.auth import TokenStore
    from bruin_mcp_service.server import build_server
    settings = load_settings(base_env)
    settings.token_file.parent.mkdir(parents=True)
    settings.token_file.write_text('{"tokens": []}')
    os.chmod(settings.token_file, 0o600)
    meta = build_server(settings, TokenStore(settings.token_file))._tool_manager._tools["validate_table_plan"].fn_metadata
    broken = json.dumps(STEPS)[:-1]
    args = {"dataset_id": "ds_0000000000000000", "steps": broken}
    passed = meta.arg_model.model_validate(meta.pre_parse_json(args)).model_dump_one_level()
    assert passed["steps"] == broken                                         # reaches the tool, not refused upstream
    good = meta.arg_model.model_validate(meta.pre_parse_json({**args, "steps": json.dumps(STEPS)}))
    assert good.model_dump_one_level()["steps"] == STEPS                     # a valid string still becomes a list
    with pytest.raises(ValueError, match=r"steps is a JSON string that does not parse: .* at character \d+ .*"
                                         r"send steps as a JSON array"):
        m.json_argument("steps", broken)
    assert m.json_argument("steps", STEPS) is STEPS


@needs_bruin
def test_steps_received_lists_every_step_and_compile_builds_exactly_the_draft(settings):
    ds = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]
    flat = {**FLAT, "dataset_id": ds}
    report = plans.validate(settings, "alice", m.parse_table_plan(flat))
    # warnings come before the long parts, so a client that cuts a long reply keeps them
    keys = list(report)
    assert keys.index("warnings") < keys.index("steps_received") < keys.index("assets")
    assert any("TRY_CAST" in w for w in report["warnings"])
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


@needs_bruin
def test_validate_and_compile_replies_are_brief_and_get_pipeline_has_the_detail(settings):
    # a 4-row join took 2.87M input tokens: each reply carried every asset's SQL and the
    # columns after every step, and the client re-sends earlier replies on each turn
    ds = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]
    flat = {**FLAT, "dataset_id": ds, "checks": [{"column": "order_id", "check": "unique"}]}
    report = plans.validate(settings, "alice", m.parse_table_plan(flat))
    reply = plans.validate_reply(report)
    assert list(reply)[:5] == ["valid", "draft_id", "plan", "warnings", "steps_received"]
    assert reply["steps_received"] == report["steps_received"] and reply["warnings"] == report["warnings"]
    assert reply["assets"]["raw.orders"] == {"kind": "raw", "columns": 5}
    assert "order_id BIGINT (unique)" in reply["assets"]["clean.orders"]["columns"]
    record = plans.build(settings, "alice", plans.load_draft(settings, "alice", report["draft_id"]))
    built = plans.compile_reply(record)
    assert built["pipeline_id"] == record["pipeline_id"] and built["bruin_validate"] == "passed"
    for short, full in ((reply, report), (built, record)):
        text = json.dumps(short)
        assert "SELECT" not in text and str(settings.workspace_root) not in text
        assert len(text) < len(json.dumps(full)) / 3                     # 10–12% on a 23-column file
    shown = plans.public(settings, plans.get(settings, "alice", record["pipeline_id"]))
    assert "CREATE TABLE clean.orders AS WITH" in shown["assets"]["clean.orders"]["rendered"]
    assert "<workspace>/users/alice/datasets/" in shown["assets"]["raw.orders"]["sql"]
    assert str(settings.workspace_root) not in json.dumps(shown)


@needs_bruin
def test_values_a_mapping_leaves_behind_that_look_like_mapped_ones_are_reported(settings):
    # live run: 'Undergraduate' was mapped to "Bachelor's" and 3 rows of 'undergrad' stayed
    rows = ["id,education,city"] + [f"{i},{e},{c}" for i, (e, c) in enumerate(
        [("Undergraduate", "Pune")] * 5 + [("undergrad", "pune")] * 3 + [("Bachelors", "Delhi")] * 2 +
        [("Diploma", "Goa")] * 4 + [("Masters", "Delhi")] + [("Master's", "Goa")])]
    ds = datasets.upload(settings, "alice", "edu.csv", b64("\n".join(rows) + "\n"))["dataset_id"]
    plan = m.parse_table_plan({"dataset_id": ds, "table": "people", "steps": [
        {"op": "replace_values", "columns": ["education"],
         "mapping": {"Undergraduate": "Bachelor's", "Masters": "Master's"}}]})
    left = [w for w in plans.validate(settings, "alice", plan)["warnings"] if "still holds" in w]
    assert left == ["clean.people step 1 (replace_values): after this mapping 'education' still holds 'undergrad' "
                    "(3 rows; looks like 'Undergraduate', mapped to \"Bachelor's\"), 'Bachelors' (2 rows; looks like "
                    "\"Bachelor's\", a value this mapping produces); add them to the mapping if they mean the same"], left
    whole = m.parse_table_plan({"dataset_id": ds, "table": "people", "steps": [
        {"op": "replace_values", "columns": ["education"], "mapping": {
            "Undergraduate": "Bachelor's", "undergrad": "Bachelor's", "Bachelors": "Bachelor's", "Masters": "Master's"}}]})
    assert not [w for w in plans.validate(settings, "alice", whole)["warnings"] if "still holds" in w]


def test_a_derived_column_with_one_value_in_every_row_is_reported(settings):
    # live run: classification_correct compared two identical label columns — 1 in all
    # 20,000 rows — and the summary called it a feature
    rows = ["id,label,truth,score"] + [f"{i},{l},{l},{i % 3}" for i, l in enumerate("abcabcab")]
    ds = datasets.upload(settings, "alice", "labels.csv", b64("\n".join(rows) + "\n"))["dataset_id"]
    plan = m.parse_table_plan({"dataset_id": ds, "table": "t", "steps": [
        {"op": "derive", "name": "correct", "expression": "CASE WHEN label = truth THEN 1 ELSE 0 END"},
        {"op": "derive", "name": "high", "expression": "score = '2'"},
        {"op": "derive", "name": "nothing", "expression": "NULL", "type": "INTEGER"}]})
    got = [w for w in plans.validate(settings, "alice", plan)["warnings"] if "carries no information" in w]
    assert got == ["clean.t step 1 (derive): derive 'correct' is '1' in all 8 rows, so it carries no information; "
                   "check the columns it uses (are they identical?)",
                   "clean.t step 3 (derive): derive 'nothing' is NULL in all 8 rows, so it carries no information; "
                   "check the columns it uses (are they identical?)"], got


def test_a_cast_or_parse_date_that_turns_values_into_null_is_reported_at_validation(settings):
    # live run: dates like '2023-09-18' parsed with '%Y-%m-%dT%H:%M:%SZ' passed validation,
    # and the run made all 20,000 of them NULL
    rows = ["id,day,amount,email"] + [f"{i},2024-01-0{i},{a},p{i}@example.org"
                                      for i, a in zip(range(1, 7), ["5", "6", "x", "7", "", "8"])]
    ds = datasets.upload(settings, "alice", "dates.csv", b64("\n".join(rows) + "\n"))["dataset_id"]

    def warned(steps):
        plan = m.parse_table_plan({"dataset_id": ds, "table": "t", "steps": steps})
        return [w for w in plans.validate(settings, "alice", plan)["warnings"] if "values do not convert and will" in w]

    assert warned([{"op": "parse_date", "columns": ["day"], "formats": ["%Y-%m-%dT%H:%M:%SZ"]}]) == [
        "clean.t step 1 (parse_date): 'day': all 6 of 6 values do not convert and will become NULL "
        "(e.g. '2024-01-01', '2024-01-06'); check the type or the format(s) against these values"]
    assert warned([{"op": "parse_date", "columns": ["day"], "formats": ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"]}]) == []
    # a missing value is not a failure; 'x' is
    assert warned([{"op": "cast", "columns": {"amount": "INTEGER"}}]) == [
        "clean.t step 1 (cast): 'amount': 1 of 5 values do not convert and will become NULL (e.g. 'x'); "
        "check the type or the format(s) against these values"]
    # personal values are not shown
    assert warned([{"op": "cast", "columns": {"email": "INTEGER"}}]) == [
        "clean.t step 1 (cast): 'email': all 6 of 6 values do not convert and will become NULL; "
        "check the type or the format(s) against these values"]


def test_numbers_filled_into_missing_values_and_checks_that_will_fail_are_reported(settings):
    # live run: a not_null check on age failed at run time (23 missing); the client then
    # filled them with 0, and its summary's average ages fell, one to 0.0
    rows = ["id,age,tier,code"] + [f"{i},{a},{t},{c}" for i, (a, t, c) in enumerate(
        [("30", "gold", "A1"), ("", "gold", "A1"), ("41", "silver", "B2"), ("", "tin", "C3"), ("-5", None, "D4")], 1)]
    ds = datasets.upload(settings, "alice", "people.csv",
                         b64("\n".join(r.replace("None", "") for r in rows) + "\n"))["dataset_id"]

    def warned(steps, checks, words):
        plan = m.parse_table_plan({"dataset_id": ds, "table": "p", "steps": steps, "checks": checks})
        return [w for w in plans.validate(settings, "alice", plan)["warnings"] if words in w]

    cast = {"op": "cast", "columns": {"age": "INTEGER"}}
    # a check this data fails is an error, not a warning: the run would fail for certain
    # (live: the warning was one of ten, the client compiled anyway and the run failed)
    plan = m.parse_table_plan({"dataset_id": ds, "table": "p", "steps": [cast], "checks": [
        {"column": "age", "check": "not_null"}, {"column": "code", "check": "unique"},
        {"column": "tier", "check": "accepted_values", "value": ["gold", "silver"]},
        {"column": "age", "check": "positive"}, {"column": "id", "check": "unique"}]})
    with pytest.raises(PlanError) as err:
        plans.validate(settings, "alice", plan)
    assert err.value.problems == [
        "clean.p checks: check not_null on 'age' fails on this data — 2 of 5 rows, so the run would fail; these "
        "values are missing — if they are truly unknown, drop the check rather than filling in a made-up value",
        "clean.p checks: check positive on 'age' fails on this data — 1 of 5 rows (e.g. '-5'), so the run would fail",
        "clean.p checks: check unique on 'code' fails on this data — 1 of 5 rows repeat a value (e.g. 'A1'), so the "
        "run would fail",
        "clean.p checks: check accepted_values on 'tier' fails on this data — 1 of 5 rows (e.g. 'tin'), so the run "
        "would fail"]
    fill = {"op": "fill_null", "columns": ["age"], "value": 0}
    assert warned([cast, fill], [{"column": "age", "check": "not_null"}], "missing values will become") == [
        "clean.p step 2 (fill_null): 'age': 2 missing values will become 0, which averages, sums and models treat "
        "as real values of 'age'; leave them NULL (averages and counts skip NULL) unless what comes next cannot "
        "take NULL"]
    assert warned([cast, fill], [{"column": "age", "check": "not_null"}], "fails on this data") == []
    # a text placeholder for a missing category is fine
    assert warned([{"op": "fill_null", "columns": ["tier"], "value": "unknown"}], [], "missing values") == []
