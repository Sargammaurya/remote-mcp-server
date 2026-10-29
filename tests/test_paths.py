import os

import pytest

from bruin_mcp_service.paths import PathError, resolve_user_path, user_root


@pytest.fixture
def ws(tmp_path):
    for user in ("alice", "bob"):
        (tmp_path / "users" / user / "uploads").mkdir(parents=True)
    (tmp_path / "users" / "bob" / "uploads" / "secret.csv").write_text("x\n")
    return tmp_path


def test_inside_path_resolves(ws):
    p = resolve_user_path(ws, "alice", "uploads/data.csv")
    assert p == (ws / "users" / "alice" / "uploads" / "data.csv").resolve()


@pytest.mark.parametrize("user_id", ["", "Alice", "../bob", "a/b", "-x", "a" * 65, None])
def test_invalid_user_ids(ws, user_id):
    with pytest.raises(PathError):
        user_root(ws, user_id)


@pytest.mark.parametrize("rel", [
    "../bob/uploads/secret.csv",
    "uploads/../../bob/uploads/secret.csv",
    "/etc/passwd",
    "",
    "   ",
    "a\x00b",
])
def test_escapes_refused(ws, rel):
    with pytest.raises(PathError):
        resolve_user_path(ws, "alice", rel)


def test_symlink_to_other_user_refused(ws):
    os.symlink(ws / "users" / "bob" / "uploads" / "secret.csv",
               ws / "users" / "alice" / "uploads" / "link.csv")
    with pytest.raises(PathError, match="escapes"):
        resolve_user_path(ws, "alice", "uploads/link.csv")


def test_symlinked_dir_escape_refused(ws):
    os.symlink(ws / "users" / "bob", ws / "users" / "alice" / "bobdir")
    with pytest.raises(PathError, match="escapes"):
        resolve_user_path(ws, "alice", "bobdir/uploads/secret.csv")
