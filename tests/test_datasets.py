import os
import stat

import pytest

from bruin_mcp_service import datasets
from bruin_mcp_service.config import load_settings
from bruin_mcp_service.datasets import DatasetError
from synthetic import MESSY_ORDERS_CSV, b64, xlsx_bytes


@pytest.fixture
def settings(base_env):
    return load_settings(base_env)


def _user_dir(settings, user):
    return settings.workspace_root / "users" / user


def test_csv_upload_list_get_delete(settings):
    meta = datasets.upload(settings, "alice", "synthetic_orders.csv", b64(MESSY_ORDERS_CSV))
    assert meta["kind"] == "csv" and meta["bytes"] == len(MESSY_ORDERS_CSV.encode())
    ds = datasets.get(settings, "alice", meta["dataset_id"])
    assert ds.data_csv.read_text(encoding="utf-8") == MESSY_ORDERS_CSV
    assert [m["dataset_id"] for m in datasets.list_all(settings, "alice")] == [meta["dataset_id"]]
    datasets.delete(settings, "alice", meta["dataset_id"])
    assert datasets.list_all(settings, "alice") == []


def test_dataset_is_read_only_on_disk(settings):
    meta = datasets.upload(settings, "alice", "a.csv", b64("x\n1\n"))
    ds = datasets.get(settings, "alice", meta["dataset_id"])
    assert stat.S_IMODE(os.stat(ds.dir).st_mode) == 0o500
    assert all(stat.S_IMODE(os.stat(f).st_mode) == 0o400 for f in ds.dir.iterdir())
    if os.getuid() != 0:  # root ignores modes
        with pytest.raises(PermissionError):
            (ds.dir / "new.csv").write_text("x")


def test_upload_name_is_only_a_label(settings):
    meta = datasets.upload(settings, "alice", "../../bob/evil.csv", b64("x\n1\n"))
    assert meta["filename"] == "evil.csv"
    assert not (settings.workspace_root / "users" / "bob").exists()


@pytest.mark.parametrize("name", ["data.parquet", "data", "x.csv.exe"])
def test_bad_extension(settings, name):
    with pytest.raises(DatasetError, match="not allowed"):
        datasets.upload(settings, "alice", name, b64("x\n1\n"))


def test_missing_extension_says_what_to_fix(settings):
    with pytest.raises(DatasetError, match=r"must end in one of \['\.csv', '\.xlsx'\] \(got 'customers'\)"):
        datasets.upload(settings, "alice", "customers", b64("x\n1\n"))


def test_invalid_base64(settings):
    with pytest.raises(DatasetError, match="base64"):
        datasets.upload(settings, "alice", "a.csv", "not base64!!")


def test_non_utf8_csv(settings):
    with pytest.raises(DatasetError, match="UTF-8"):
        datasets.upload(settings, "alice", "a.csv", b64("name\ncaf\xe9\n".encode("latin-1")))


def test_size_limit(base_env):
    s = load_settings({**base_env, "BMCP_MAX_UPLOAD_MB": "1"})
    with pytest.raises(DatasetError, match="1 MB"):
        datasets.upload(s, "alice", "a.csv", b64("x\n" + "1\n" * 600_000))


def test_quota(base_env, monkeypatch):
    s = load_settings(base_env)
    monkeypatch.setattr(datasets, "_dir_bytes", lambda _p: s.user_quota_bytes - 5)
    with pytest.raises(DatasetError, match="quota"):
        datasets.upload(s, "alice", "a.csv", b64("x\n1\n"))


def test_failed_upload_leaves_nothing(settings):
    with pytest.raises(DatasetError):
        datasets.upload(settings, "alice", "a.csv", b64(b"\x00\x00"))
    assert list((_user_dir(settings, "alice") / "datasets").iterdir()) == []


def test_xlsx_first_and_named_sheet(settings):
    data = xlsx_bytes({"First": [["a", "b"], [1, None], [2, "x"]], "Second": [["k"], ["v"]]})
    first = datasets.upload(settings, "alice", "book.xlsx", b64(data))
    assert first["sheet"] == "First"
    ds = datasets.get(settings, "alice", first["dataset_id"])
    assert ds.data_csv.read_text().splitlines() == ["a,b", "1,", "2,x"]
    second = datasets.upload(settings, "alice", "book.xlsx", b64(data), sheet="Second")
    assert second["sheet"] == "Second"
    with pytest.raises(DatasetError, match="not found"):
        datasets.upload(settings, "alice", "book.xlsx", b64(data), sheet="Nope")


def test_corrupt_xlsx(settings):
    with pytest.raises(DatasetError, match="not a valid .xlsx"):
        datasets.upload(settings, "alice", "book.xlsx", b64(b"PK not really a zip"))


def test_other_users_dataset_looks_missing(settings):
    meta = datasets.upload(settings, "alice", "a.csv", b64("x\n1\n"))
    for op in (datasets.get, datasets.delete):
        with pytest.raises(DatasetError, match="no dataset"):
            op(settings, "bob", meta["dataset_id"])
    assert datasets.list_all(settings, "bob") == []
    assert datasets.get(settings, "alice", meta["dataset_id"])


@pytest.mark.parametrize("bad", ["../alice", "ds_../../x", "ds_XYZ", "", None, "meta.json"])
def test_malformed_dataset_ids(settings, bad):
    with pytest.raises(DatasetError, match="no dataset"):
        datasets.get(settings, "alice", bad)
