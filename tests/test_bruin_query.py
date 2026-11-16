"""Guard and inspection tests against the real pinned Bruin (synthetic data)."""

import pytest

from bruin_mcp_service import datasets
from bruin_mcp_service.bruin_query import QueryError, guarded_query, sql_string
from bruin_mcp_service.config import load_settings
from bruin_mcp_service.inspect_data import inspect
from conftest import needs_bruin
from synthetic import MESSY_EXPECTED, MESSY_ORDERS_CSV, b64

pytestmark = needs_bruin


@pytest.fixture
def settings(real_env):
    return load_settings(real_env)


@pytest.fixture
def two_users(settings, tmp_path):
    a = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))
    b = datasets.upload(settings, "bob", "synthetic_secret.csv", b64("secret\nBOB-ONLY\n"))
    return datasets.get(settings, "alice", a["dataset_id"]), datasets.get(settings, "bob", b["dataset_id"])


def test_inspection_matches_independent_counts(settings, two_users):
    alice, _ = two_users
    out = inspect(settings, alice, sample_rows=3)
    assert out["row_count"] == MESSY_EXPECTED["row_count"]
    assert out["duplicate_rows"] == MESSY_EXPECTED["duplicate_rows"]
    cols = {c["name"]: c for c in out["columns"]}
    assert list(cols) == ["order_id", "customer", "amount", "order_date", "country"]
    assert {n: c["null_count"] for n, c in cols.items()} == MESSY_EXPECTED["nulls"]
    assert cols["order_id"]["distinct_count"] == MESSY_EXPECTED["distinct_order_id"]
    assert cols["order_id"]["type"] == "BIGINT"
    assert cols["amount"]["type"] == "VARCHAR"      # "abc" and "  300 " are present
    assert cols["order_date"]["type"] == "VARCHAR"  # mixed formats, impossible dates
    assert len(out["sample"]["rows"]) == 3


def test_guard_blocks_other_users_folder(settings, two_users):
    alice, bob = two_users
    with pytest.raises(QueryError, match="disabled by configuration"):
        guarded_query(settings, alice.dir, f"SELECT * FROM read_csv({sql_string(str(bob.data_csv))})")


def test_guard_blocks_traversal(settings, two_users):
    alice, bob = two_users
    sneaky = f"{alice.dir}/../../../bob/datasets/{bob.dataset_id}/data.csv"
    with pytest.raises(QueryError, match="disabled by configuration"):
        guarded_query(settings, alice.dir, f"SELECT * FROM read_csv({sql_string(sneaky)})")


def test_guard_blocks_urls_and_system_files(settings, two_users):
    alice, _ = two_users
    for target in ("https://example.com/x.csv", "/etc/passwd"):
        with pytest.raises(QueryError, match="disabled by configuration"):
            guarded_query(settings, alice.dir, f"SELECT * FROM read_csv({sql_string(target)})")


def test_guard_cannot_be_reopened(settings, two_users):
    alice, _ = two_users
    for stmt in ("SET enable_external_access=true; SELECT 1",
                 "SET allowed_directories=['/']; SELECT 1",
                 "SET lock_configuration=false; SELECT 1",
                 "INSTALL httpfs; SELECT 1"):
        with pytest.raises(QueryError):
            guarded_query(settings, alice.dir, stmt)


def test_write_inside_dataset_blocked_by_read_only_folder(settings, two_users):
    """allowed_directories alone permits writes; the 0500 folder stops them (non-root only)."""
    import os
    if os.getuid() == 0:
        pytest.skip("root ignores file modes; run the suite as the service user")
    alice, _ = two_users
    target = sql_string(str(alice.dir / "out.csv"))
    with pytest.raises(QueryError):
        guarded_query(settings, alice.dir, f"COPY (SELECT 1) TO {target}; SELECT 1")
    assert not (alice.dir / "out.csv").exists()


def test_bruin_error_json_with_exit_0_is_an_error(settings, two_users):
    alice, _ = two_users
    with pytest.raises(QueryError, match="does_not_exist"):
        guarded_query(settings, alice.dir, "SELECT does_not_exist FROM (SELECT 1 AS a)")


def test_scratch_dirs_removed(settings, two_users):
    alice, _ = two_users
    guarded_query(settings, alice.dir, "SELECT 1")
    assert list(settings.tmp_dir.iterdir()) == []


# ─── resource limits (H1: one inspect of a 3,001-column file used 25 GB) ──────

def test_the_guard_caps_memory_threads_and_spill_and_locks_them(settings, two_users):
    alice, _ = two_users
    got = guarded_query(settings, alice.dir, "SELECT current_setting('memory_limit'), current_setting('threads'), "
                                             "current_setting('max_temp_directory_size')")["rows"][0]
    assert got == ["1.9 GiB", 4, "9.3 GiB"]                       # 2048 MB, 4 threads, 10 GB as DuckDB shows them
    with pytest.raises(QueryError, match="Cannot change configuration"):
        guarded_query(settings, alice.dir, "SET memory_limit='20GB'; SELECT 1")


def test_a_query_over_the_memory_limit_spills_and_one_that_cannot_fit_says_why(settings, two_users):
    import dataclasses
    alice, _ = two_users
    small = dataclasses.replace(settings, query_memory_mb=256)
    got = guarded_query(small, alice.dir,
                        "SELECT count(*) FROM (SELECT DISTINCT i::VARCHAR || 'x' FROM range(20000000) t(i))")
    assert got["rows"] == [[20000000]]                            # needs far more than 256 MB: spilled
    tiny = dataclasses.replace(settings, query_memory_mb=64)
    with pytest.raises(QueryError, match=r"more than the service's 64 MB per query \(BMCP_QUERY_MEMORY_MB\)"):
        guarded_query(tiny, alice.dir, "SELECT list(i) FROM range(50000000) t(i)")


def test_sql_too_long_for_one_argument_is_a_query_error(settings, two_users):
    alice, _ = two_users
    with pytest.raises(QueryError, match="too long"):
        guarded_query(settings, alice.dir, "SELECT " + " + ".join(["1"] * 70000))


def _inspect_csv(settings, text, name="data.csv"):
    meta = datasets.upload(settings, "alice", name, b64(text))
    return inspect(settings, datasets.get(settings, "alice", meta["dataset_id"]))


def test_counts_are_exact_with_duplicates_empty_columns_and_no_rows(settings):
    rows = ["id,cat,empty,num"] + [f"{i % 50},c{i % 3},,{i * 0.5}" for i in range(400)] + ["0,c0,,0.0"] * 3
    out = _inspect_csv(settings, "\n".join(rows) + "\n")
    cols = {c["name"]: c for c in out["columns"]}
    assert out["row_count"] == 403 and out["duplicate_rows"] == 3
    assert [cols[n]["distinct_count"] for n in ("id", "cat", "empty", "num")] == [50, 3, 0, 400]
    assert [cols[n]["null_count"] for n in ("id", "cat", "empty", "num")] == [0, 0, 403, 0]
    assert [v["count"] for v in cols["cat"]["values"]["values"]] == [137, 133, 133]
    header_only = _inspect_csv(settings, "a,b\n", "header_only.csv")
    assert header_only["row_count"] == 0 and [c["distinct_count"] for c in header_only["columns"]] == [0, 0]


def test_a_wide_file_is_inspected_in_batches_with_the_same_counts(settings, monkeypatch):
    from bruin_mcp_service import inspect_data
    names = [f"column_with_a_long_name_{i:03d}_" + "x" * 60 for i in range(120)]
    rows = [",".join(names)] + [",".join(f"v{(r * (i + 1)) % (i + 2)}" for i in range(120)) for r in range(300)]
    text = "\n".join(rows) + "\n"
    whole = _inspect_csv(settings, text, "wide.csv")
    calls = []
    real = inspect_data.guarded_query
    monkeypatch.setattr(inspect_data, "guarded_query", lambda *a, **k: calls.append(a[2]) or real(*a, **k))
    monkeypatch.setattr(inspect_data, "MAX_STATEMENT", 4000)
    batched = _inspect_csv(settings, text, "wide2.csv")
    assert max(len(sql) for sql in calls) < 4000 + 2000 and len(calls) > 6       # really split
    strip = lambda r: [{k: v for k, v in c.items()} for c in r["columns"]]
    assert strip(batched) == strip(whole) and batched["row_count"] == whole["row_count"] == 300
    assert [c["distinct_count"] for c in whole["columns"]][:3] == [2, 3, 4]
