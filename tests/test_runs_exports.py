"""
Phase 6 against the real pinned Bruin: integrity gate, sandboxed run, results,
exports and chunked download. SYNTHETIC data only.
"""

import base64
import csv
import dataclasses
import hashlib
import io
import os
import subprocess
import sys
import time

import pytest

from bruin_mcp_service import datasets, sandbox
from bruin_mcp_service.bruin_cli import bruin_env
from bruin_mcp_service.config import load_settings
from bruin_mcp_service.plan import exports, runs
from bruin_mcp_service.plan import pipeline as plans
from conftest import needs_bruin
from synthetic import MESSY_ORDERS_CSV, b64

pytestmark = needs_bruin


def _build(settings, user, plan_data):
    """Parse then build — what compile_plan does with a stored draft."""
    from bruin_mcp_service.plan.model import parse_plan
    return plans.build(settings, user, parse_plan(plan_data))


@pytest.fixture
def settings(real_env):
    return load_settings(real_env)


def _plan(dataset_id, checks=None):
    return {"name": "orders_prep", "sources": [{"table": "orders", "dataset_id": dataset_id}],
            "clean": [{"table": "orders", "steps": [
                {"op": "trim", "columns": ["customer", "country", "amount"]},
                {"op": "change_case", "columns": ["country"], "to": "upper"},
                {"op": "null_if", "columns": ["customer", "country", "amount"], "values": [""]},
                {"op": "cast", "columns": {"order_id": "BIGINT", "amount": "DOUBLE"}},
                {"op": "dedupe", "key": ["order_id"], "order_by": ["order_id"]}],
                "checks": checks if checks is not None else [{"column": "order_id", "check": "unique"}]}],
            "outputs": [{"kind": "aggregate", "name": "by_country", "source": "clean.orders",
                         "group_by": ["country"], "measures": [{"name": "n", "fn": "count"}]}]}


@pytest.fixture
def built(settings):
    ds = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]
    return _build(settings, "alice", _plan(ds)), ds


def _wait(settings, user, pid, run_id, limit=180):
    deadline = time.time() + limit
    while time.time() < deadline:
        run = runs.get(settings, user, pid, run_id)
        if run["status"] != "running":
            return run
        time.sleep(0.3)
    raise AssertionError("run did not finish")


def _root(settings, pid, user="alice"):
    return settings.workspace_root / "users" / user / "pipelines" / pid


# ─── runs ─────────────────────────────────────────────────────────────────────

def test_a_run_succeeds_and_reports_assets_checks_and_tables(settings, built):
    record, _ = built
    started = runs.start(settings, "alice", record["pipeline_id"])
    assert started["status"] == "running"
    run = _wait(settings, "alice", record["pipeline_id"], started["run_id"])
    assert run["status"] == "succeeded" and run["exit_code"] == 0, run
    assert run["assets"] == {"raw.orders": "pass", "clean.orders": "pass", "mart.by_country": "pass"}
    assert run["checks"].get("clean.orders:order_id:unique") == "passed"
    assert run["tables"]["clean.orders"]["rows"] == 10 and run["tables"]["raw.orders"]["rows"] == 12
    assert [c["name"] for c in run["tables"]["clean.orders"]["columns"]][:2] == ["order_id", "customer"]
    assert str(settings.workspace_root) not in run["log_tail"]


def test_a_failing_check_fails_the_run_with_the_reason(settings):
    ds = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))["dataset_id"]
    record = _build(settings, "alice", _plan(ds, checks=[
        {"column": "country", "check": "accepted_values", "value": ["IN", "US"]}]))
    run = _wait(settings, "alice", record["pipeline_id"],
                runs.start(settings, "alice", record["pipeline_id"])["run_id"])
    assert run["status"] == "failed" and run["exit_code"] != 0
    assert any("accepted_values" in e or "country" in e for e in run["errors"] + [run["log_tail"]])


@pytest.mark.parametrize("tamper", ["edit_asset", "extra_asset", "edit_config"])
def test_tampered_pipelines_are_refused_before_bruin_runs(settings, built, tamper):
    record, _ = built
    root = _root(settings, record["pipeline_id"])
    if tamper == "edit_asset":
        path = root / "pipeline/assets/clean/orders.sql"
        path.write_text(path.read_text() + "\n-- edited\n")
    elif tamper == "extra_asset":
        (root / "pipeline/assets/clean/evil.sql").write_text("/* @bruin\nname: clean.evil\n@bruin */\nSELECT 1\n")
    else:
        (root / ".bruin.yml").write_text((root / ".bruin.yml").read_text() + "\n")
    with pytest.raises(runs.RunError, match="pipeline refused"):
        runs.start(settings, "alice", record["pipeline_id"])
    assert not (root / "warehouse.duckdb").exists()


def test_the_sandbox_denies_other_users_data_and_service_secrets(settings, built):
    """Bypass the gate on purpose and plant reads: the OS must refuse them."""
    record, ds = built
    other = datasets.upload(settings, "bob", "secret.csv", b64("secret\nBOB-ONLY\n"))
    other_csv = settings.workspace_root / "users/bob/datasets" / other["dataset_id"] / "data.csv"
    secret = settings.token_file.parent / "planted-secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("TOKEN-FILE-STAND-IN\n")
    root = _root(settings, record["pipeline_id"])
    for i, target in enumerate((other_csv, secret)):
        (root / f"pipeline/assets/clean/evil{i}.sql").write_text(
            f"/* @bruin\nname: clean.evil{i}\ntype: duckdb.sql\nmaterialization:\n  type: table\n@bruin */\n"
            f"SELECT * FROM read_csv('{target}', header=false, all_varchar=true)\n")
    ds_dir = settings.workspace_root / "users/alice/datasets" / ds
    cmd = sandbox.command(sys.executable, [str(settings.bruin_bin), "run", "--config-file",
                                           str(root / ".bruin.yml"), "--no-color", str(root / "pipeline")],
                          ro=[settings.bruin_bin.parent, settings.bruin_home, ds_dir], rw=[root])
    out = subprocess.run(cmd, cwd=root, env=bruin_env(settings), capture_output=True, text=True, timeout=300)
    text = out.stdout + out.stderr
    assert out.returncode != 0
    assert text.count("Permission denied") >= 2 and "BOB-ONLY" not in text and "TOKEN-FILE" not in text
    assert "PASS clean.orders" in text                            # the legitimate asset still ran


def test_the_launcher_caps_the_commands_memory():
    probe = ["/usr/bin/python3", "-I", "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_DATA))"]
    out = subprocess.run(sandbox.command(sys.executable, probe, data_mb=512), capture_output=True, text=True,
                         timeout=60)
    assert out.stdout.strip() == f"({512 * 2 ** 20}, {512 * 2 ** 20})"
    bad = subprocess.run(sandbox.command(sys.executable, probe, data_mb=0), capture_output=True, text=True,
                         timeout=60)
    assert bad.returncode == 2 and "refusing to run" in bad.stderr and bad.stdout == ""


def test_a_run_over_its_memory_limit_fails_cleanly_and_says_why(settings, built, monkeypatch):
    """A real Bruin asset over --data-mb fails with DuckDB's own error (it used 26 GB
    unlimited); runs.start then names the limit."""
    record, ds = built
    root = _root(settings, record["pipeline_id"])
    (root / "pipeline/assets/clean/heavy.sql").write_text(
        "/* @bruin\nname: clean.heavy\ntype: duckdb.sql\nmaterialization:\n  type: table\n@bruin */\n"
        "SELECT count(DISTINCT i::VARCHAR || '-pad-pad') AS n FROM range(100000000) t(i)\n")
    ds_dir = settings.workspace_root / "users/alice/datasets" / ds
    cmd = sandbox.command(sys.executable, [str(settings.bruin_bin), "run", "--config-file",
                                           str(root / ".bruin.yml"), "--no-color", str(root / "pipeline")],
                          ro=[settings.bruin_bin.parent, settings.bruin_home, ds_dir], rw=[root], data_mb=1024)
    started = time.monotonic()
    out = subprocess.run(cmd, cwd=root, env=bruin_env(settings), capture_output=True, text=True, timeout=300)
    assert out.returncode != 0 and "Out of Memory" in out.stdout + out.stderr
    assert time.monotonic() - started < 120
    (root / "pipeline/assets/clean/heavy.sql").unlink()

    fake = subprocess.CompletedProcess([], 1, "FAIL clean.orders\n└── Out of Memory Error: Allocation failure\n", "")
    monkeypatch.setattr(runs.subprocess, "run", lambda *a, **k: fake)
    run = _wait(settings, "alice", record["pipeline_id"], runs.start(settings, "alice", record["pipeline_id"])["run_id"])
    assert run["status"] == "failed"
    assert run["errors"][0].startswith("the run needed more than the service's 4096 MB per run (BMCP_RUN_MEMORY_MB)")


def test_a_run_reports_exactly_what_it_changed(settings):
    # live run: "verified via sample comparison" that satisfaction was unchanged; all
    # 100 values had gone from 'True'/'False' to 'true'/'false', and a trim changed nothing
    csv_text = "id,label,flag,amount,delay\n1, a ,True,5,0.0\n2,b,False,x,0.0\n3,b,True,2.5,7\n3,b,True,2.5,7\n"
    ds = datasets.upload(settings, "alice", "flags.csv", b64(csv_text))["dataset_id"]
    record = _build(settings, "alice", {
        "name": "flags", "sources": [{"table": "t", "dataset_id": ds}],
        "clean": [{"table": "t", "steps": [
            {"op": "trim", "columns": ["label", "flag"]},
            {"op": "rename", "mapping": {"label": "tag"}},
            {"op": "cast", "columns": {"id": "INTEGER", "flag": "BOOLEAN", "amount": "INTEGER", "delay": "BIGINT"}},
            {"op": "dedupe"},
            {"op": "filter", "where": {"column": "id", "operator": "ne", "value": 2}}]}]})
    run = _wait(settings, "alice", record["pipeline_id"], runs.start(settings, "alice", record["pipeline_id"])["run_id"])
    assert run["status"] == "succeeded"
    got = run["changes"]["clean.t"]
    assert got["rows"] == {"before": 4, "after": 2, "removed_by": {"step 4 (dedupe)": 1, "step 5 (filter)": 1}}
    assert got["renamed"] == {"label": "tag"} and "dropped" not in got and "added" not in got
    assert got["retyped"] == {"id": "VARCHAR → INTEGER", "flag": "VARCHAR → BOOLEAN", "amount": "VARCHAR → INTEGER",
                             "delay": "VARCHAR → BIGINT"}
    by = {(e["step"], e["column"]): e for e in got["by_step"]}
    assert by[("1 (trim)", "label")] == {"step": "1 (trim)", "column": "label", "rows_changed": 1,
                                         "examples": [{"from": " a ", "to": "a", "rows": 1}]}
    assert by[("1 (trim)", "flag")]["rows_changed"] == 0                           # said, not implied
    assert by[("3 (cast)", "flag")]["became_null"] == 0 and by[("3 (cast)", "flag")]["written_differently"] == 4
    assert by[("3 (cast)", "flag")]["examples"] == [{"from": "True", "to": "true", "rows": 3},
                                                    {"from": "False", "to": "false", "rows": 1}]
    amount = by[("3 (cast)", "amount")]
    assert amount["became_null"] == 3 and amount["written_differently"] == 0        # '2.5' is not rounded to 3
    assert amount["not_converted"] == [{"from": "2.5", "to": None, "rows": 2}, {"from": "x", "to": None, "rows": 1}]
    assert by[("3 (cast)", "id")]["written_differently"] == 0 and "reformatted" not in by[("3 (cast)", "id")]
    delay = by[("3 (cast)", "delay")]                     # live: 100 cells '0.0' → '0' went unreported
    assert delay["written_differently"] == 0 and delay["reformatted"] == 2
    assert delay["reformatted_examples"] == [{"from": "0.0", "to": "0", "rows": 2}]


def test_a_report_that_cannot_be_made_does_not_change_the_runs_outcome(settings, built, monkeypatch):
    record, _ = built
    monkeypatch.setattr(runs, "run_changes", lambda *a: 1 / 0)
    run = _wait(settings, "alice", record["pipeline_id"], runs.start(settings, "alice", record["pipeline_id"])["run_id"])
    assert run["status"] == "succeeded" and run["changes"] == {"error": "changes could not be computed: ZeroDivisionError"}


def test_a_second_run_of_the_same_pipeline_waits_its_turn(settings, built):
    record, _ = built
    first = runs.start(settings, "alice", record["pipeline_id"])
    with pytest.raises(runs.RunError, match="already running"):
        runs.start(settings, "alice", record["pipeline_id"])
    _wait(settings, "alice", record["pipeline_id"], first["run_id"])


def test_a_run_over_its_time_limit_is_stopped(settings, built):
    record, _ = built
    quick = dataclasses.replace(settings, run_timeout_s=0.01)
    run = _wait(settings, "alice", record["pipeline_id"],
                runs.start(quick, "alice", record["pipeline_id"])["run_id"])
    assert run["status"] == "timeout" and "exceeded" in run["errors"][0]


def test_runs_of_another_users_pipeline_are_not_visible(settings, built):
    record, _ = built
    with pytest.raises(plans.PipelineError):
        runs.start(settings, "bob", record["pipeline_id"])


def test_interrupted_runs_are_marked_at_startup(settings, built):
    record, _ = built
    root = _root(settings, record["pipeline_id"])
    (root / "runs").mkdir(exist_ok=True)
    (root / "runs/run_0123456789abcdef.json").write_text('{"run_id": "run_0123456789abcdef", "status": "running"}')
    runs.mark_interrupted(settings)
    assert runs.get(settings, "alice", record["pipeline_id"], "run_0123456789abcdef")["status"] == "interrupted"


def test_get_run_waits_for_a_running_run_instead_of_answering_at_once(settings, built):
    # live run: get_run answered instantly, the client polled 3 times in ~3 s, its
    # repeated-call guard fired and it abandoned a run that succeeded at 20 s
    import json
    import threading
    record, _ = built
    root = _root(settings, record["pipeline_id"])
    (root / "runs").mkdir(exist_ok=True)
    path = root / "runs/run_00000000000000aa.json"
    running = {"run_id": "run_00000000000000aa", "status": "running", "started_at": int(time.time())}
    path.write_text(json.dumps(running))

    def finish():
        time.sleep(1.5)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**running, "status": "succeeded"}))
        os.replace(tmp, path)
    threading.Thread(target=finish).start()
    started = time.monotonic()
    run = runs.get(settings, "alice", record["pipeline_id"], "run_00000000000000aa")
    assert run["status"] == "succeeded" and 1.0 < time.monotonic() - started < 10 and "next" not in run

    path.write_text(json.dumps(running))
    started = time.monotonic()
    run = runs.get(settings, "alice", record["pipeline_id"], "run_00000000000000aa", wait_s=1)
    assert run["status"] == "running" and time.monotonic() - started >= 1
    assert "call get_run again" in run["next"]
    assert "next" not in json.loads(path.read_text())                     # the stored record is unchanged
    assert runs.RUN_WAIT_S < 55                                           # under Swarn's call timeout


# ─── exports ──────────────────────────────────────────────────────────────────

def _ran(settings, built):
    record, _ = built
    _wait(settings, "alice", record["pipeline_id"], runs.start(settings, "alice", record["pipeline_id"])["run_id"])
    return record["pipeline_id"]


def test_export_needs_a_successful_run_and_a_real_asset(settings, built):
    record, _ = built
    with pytest.raises(exports.ExportError, match="no successful run"):
        exports.export(settings, "alice", record["pipeline_id"], "clean.orders")
    pid = _ran(settings, built)
    with pytest.raises(exports.ExportError, match="not an asset"):
        exports.export(settings, "alice", pid, "clean.nothing")


def test_csv_export_matches_the_table_and_downloads_in_verified_chunks(settings, built, monkeypatch):
    pid = _ran(settings, built)
    meta = exports.export(settings, "alice", pid, "clean.orders")
    assert meta["rows"] == 10 and meta["columns"][0] == "order_id"
    monkeypatch.setattr(exports, "CHUNK_BYTES", 97)               # force several chunks
    data, offset, calls = b"", 0, 0
    while offset is not None:
        part = exports.download(settings, "alice", meta["export_id"], offset)
        data += base64.b64decode(part["content_base64"])
        offset, calls = part["next_offset"], calls + 1
    assert calls > 3 and len(data) == meta["bytes"]
    assert hashlib.sha256(data).hexdigest() == meta["sha256"]
    rows = list(csv.DictReader(io.StringIO(data.decode())))
    by_id = {r["order_id"]: r for r in rows}
    assert by_id["7"]["country"] == "UK" and by_id["7"]["amount"] == "1000.0"
    assert by_id["4"]["amount"] == "" and by_id["1"]["customer"] == "Alice"


def test_xlsx_export(settings, built):
    import openpyxl
    pid = _ran(settings, built)
    meta = exports.export(settings, "alice", pid, "mart.by_country", "xlsx")
    path = settings.workspace_root / "users/alice/exports" / f"{meta['export_id']}.xlsx"
    sheet = openpyxl.load_workbook(path, read_only=True).active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0] == ("country", "n") and sum(r[1] for r in rows[1:]) == 10


def test_exports_are_per_user_and_deletable(settings, built):
    pid = _ran(settings, built)
    meta = exports.export(settings, "alice", pid, "clean.orders")
    with pytest.raises(exports.ExportError, match="no export"):
        exports.download(settings, "bob", meta["export_id"])
    assert exports.list_all(settings, "bob") == []
    assert [e["export_id"] for e in exports.list_all(settings, "alice")] == [meta["export_id"]]
    exports.delete(settings, "alice", meta["export_id"])
    assert exports.list_all(settings, "alice") == []
    with pytest.raises(exports.ExportError, match="offset"):
        meta2 = exports.export(settings, "alice", pid, "clean.orders")
        exports.download(settings, "alice", meta2["export_id"], offset=10 ** 9)


def test_export_size_cap(settings, built):
    pid = _ran(settings, built)
    tiny = dataclasses.replace(settings, max_export_mb=0)
    with pytest.raises(exports.ExportError, match="exceed"):
        exports.export(tiny, "alice", pid, "clean.orders")
    assert not list((settings.workspace_root / "users/alice/exports").glob(".staging-*"))


def test_export_result_says_to_download_but_the_stored_meta_does_not(settings, built):
    pid = _ran(settings, built)
    meta = exports.export(settings, "alice", pid, "clean.orders")
    assert f"download_export with export_id={meta['export_id']!r}" in meta["next_step"]
    stored = (settings.workspace_root / "users" / "alice" / "exports" / f"{meta['export_id']}.json").read_text()
    assert "next_step" not in stored
    assert "next_step" not in exports.list_all(settings, "alice")[0]


def test_exports_carry_bounded_column_summaries(settings, built):
    pid = _ran(settings, built)
    meta = exports.export(settings, "alice", pid, "clean.orders")
    sums = meta["column_summaries"]
    assert {v["value"] for v in sums["country"]["values"]} == {"IN", "US", "UK"}
    assert sums["country"]["nulls"] == 2
    assert sums["customer"]["values_hidden"].startswith("masked")
    assert "order_id" not in sums                                        # not a text column


@pytest.mark.parametrize("check,fails", [
    ({"column": "age", "check": "not_null"}, True), ({"column": "age", "check": "positive"}, True),
    ({"column": "age", "check": "min", "value": 0}, True), ({"column": "age", "check": "max", "value": 41}, False),
    ({"column": "code", "check": "unique"}, True), ({"column": "id", "check": "unique"}, False),
    # NULL tier: Bruin does not count it against accepted_values, and validate must agree
    ({"column": "tier", "check": "accepted_values", "value": ["gold", "silver", "tin"]}, False),
    ({"column": "tier", "check": "accepted_values", "value": ["gold", "silver"]}, True),
])
def test_validate_predicts_exactly_the_checks_bruin_fails(settings, check, fails):
    rows = "id,age,tier,code\n1,30,gold,A1\n2,,gold,A1\n3,41,silver,B2\n4,,tin,C3\n5,-5,,D4\n"
    ds = datasets.upload(settings, "alice", "people.csv", b64(rows))["dataset_id"]
    plan = {"name": "p", "sources": [{"table": "p", "dataset_id": ds}],
            "clean": [{"table": "p", "steps": [{"op": "cast", "columns": {"age": "INTEGER"}}], "checks": [check]}]}
    from bruin_mcp_service.plan.model import parse_plan
    from bruin_mcp_service.plan.engine import PlanError
    try:
        plans.validate(settings, "alice", parse_plan(plan))
        predicted = False
    except PlanError as e:
        predicted = any("fails on this data" in p for p in e.problems)
    record = _build(settings, "alice", plan)
    run = _wait(settings, "alice", record["pipeline_id"], runs.start(settings, "alice", record["pipeline_id"])["run_id"])
    assert (run["status"] == "failed") is fails and predicted is fails, (run.get("errors"), run.get("checks"))
