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
