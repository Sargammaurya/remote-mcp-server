"""Bounded value summaries (item 2): what is shown, what is hidden, and on real data."""

import pytest

from bruin_mcp_service import datasets, summaries
from bruin_mcp_service.config import load_settings
from bruin_mcp_service.inspect_data import inspect
from conftest import needs_bruin
from synthetic import MESSY_ORDERS_CSV, b64


def test_low_variety_text_columns_show_values_with_counts():
    out = summaries.summarize("country", 3, [("UK", 10), ("IN", 8), ("U.S.", 5)])
    assert out == {"distinct": 3, "values": [{"value": "UK", "count": 10}, {"value": "IN", "count": 8},
                                             {"value": "U.S.", "count": 5}]}


def test_top_values_reads_a_wide_csv_once_and_fast(tmp_path):
    # live run: 59 text columns of a 34 MB, 323-column CSV took 231 s — the file
    # was re-read once per column — and inspect_dataset timed out
    duckdb = pytest.importorskip("duckdb")
    import csv
    import time
    path = tmp_path / "wide.csv"
    names = [f"cat_{i}" for i in range(60)] + [f"num_{i}" for i in range(140)]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(names)
        for r in range(20000):
            w.writerow([f"v{(r + i) % 7}" for i in range(60)] + [r * i for i in range(140)])
    source = f"read_csv('{path}', header=true, sample_size=-1)"
    quote = lambda n: '"' + n.replace('"', '""') + '"'
    sql = summaries.top_values_sql(source, list(enumerate(names[:60])), quote)
    assert sql.count("read_csv(") == 1
    started = time.monotonic()
    rows = duckdb.connect().execute(sql).fetchall()
    assert time.monotonic() - started < 20
    assert len(rows) == 60 * 7 and {n for _, _, n in rows} <= {2857, 2858}


def test_limits_cap_what_is_listed():
    many = [(f"v{i}", 100 - i) for i in range(summaries.TOP_K + 1)]
    out = summaries.summarize("code", 30, many)
    assert len(out["values"]) == summaries.TOP_K and out["values_not_shown"] == 30 - summaries.TOP_K
    assert "values" not in summaries.summarize("city", summaries.MAX_DISTINCT + 1, [])
    long = summaries.summarize("note", 1, [("x" * 200, 1)])["values"][0]["value"]
    assert len(long) == summaries.MAX_LEN + 1 and long.endswith("…")


@pytest.mark.parametrize("column", ["email", "Phone Number", "customer", "Customer Name", "address_line",
                                    "date_of_birth", "card_number", "account", "patient", "first_name"])
def test_personal_columns_are_masked_by_name(column):
    out = summaries.summarize(column, 2, [("anything", 1)])
    assert "values" not in out and out["values_hidden"].startswith("masked")


@pytest.mark.parametrize("value", ["x@example.org", "call +91 98765 43210", "4111 1111 1111 1111"])
def test_personal_values_are_masked_even_in_an_innocent_column(value):
    out = summaries.summarize("notes", 2, [("IN", 3), (value, 1)])
    assert "values" not in out and "look like personal data" in out["values_hidden"]


@pytest.mark.parametrize("column", ["customer_segment", "client_type", "country", "plan", "status"])
def test_categories_are_not_masked(column):
    assert "values" in summaries.summarize(column, 1, [("a", 1)])


@needs_bruin
def test_inspection_shows_the_real_variants_and_masks_people(real_env):
    settings = load_settings(real_env)
    meta = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))
    report = inspect(settings, datasets.get(settings, "alice", meta["dataset_id"]))
    cols = {c["name"]: c for c in report["columns"]}
    country = {v["value"]: v["count"] for v in cols["country"]["values"]["values"]}
    # counted by hand from the synthetic file: IN in orders 1,4,8,9; US in 3,3(dup),6
    assert country == {"IN": 4, "US": 3, "in": 1, "uk": 1}            # the variants are now visible
    assert cols["customer"]["values"]["values_hidden"].startswith("masked")
    assert "values" not in cols["order_id"]                             # numbers are not listed
