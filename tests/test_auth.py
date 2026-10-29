import asyncio
import json
import os
import time

import pytest

from bruin_mcp_service import admin
from bruin_mcp_service.auth import (AuthError, TokenFileError, TokenStore, TokenStoreVerifier,
                                    caller_for_tool, read_records)
from bruin_mcp_service.tools import TOOL_NAMES


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "auth" / "tokens.json"
    monkeypatch.setenv("BMCP_TOKEN_FILE", str(path))
    admin.main(["init"])
    return path


def test_init_creates_0600_empty_file(token_file):
    assert oct(os.stat(token_file).st_mode & 0o777) == "0o600"
    assert json.loads(token_file.read_text()) == {"tokens": []}


def test_only_hash_is_stored(token_file):
    token = admin.issue(token_file, "alice", TOOL_NAMES, 90)
    text = token_file.read_text()
    assert token not in text and token.split("_", 2)[2] not in text


def test_valid_token(token_file):
    token = admin.issue(token_file, "alice", frozenset({"health"}), 90)
    record, reason = TokenStore(token_file).lookup(token)
    assert reason == "ok" and record.user_id == "alice"


@pytest.mark.parametrize("bad", ["", "x", "Bearer abc", "bmcp_000000000000_" + "A" * 43])
def test_unknown_or_malformed(token_file, bad):
    admin.issue(token_file, "alice", TOOL_NAMES, 90)
    record, reason = TokenStore(token_file).lookup(bad)
    assert record is None and reason in ("malformed", "unknown")


def test_right_id_wrong_secret(token_file):
    token = admin.issue(token_file, "alice", TOOL_NAMES, 90)
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert TokenStore(token_file).lookup(forged) == (None, "unknown")


def test_expiry(token_file):
    token = admin.issue(token_file, "alice", TOOL_NAMES, 1, now=time.time() - 2 * 86400)
    assert TokenStore(token_file).lookup(token) == (None, "expired")


def test_revocation_seen_without_restart(token_file):
    token = admin.issue(token_file, "alice", TOOL_NAMES, 90)
    store = TokenStore(token_file)
    assert store.lookup(token)[1] == "ok"
    assert admin.revoke(token_file, token.split("_")[1])
    assert store.lookup(token) == (None, "revoked")


def test_days_limit_and_unknown_tool(token_file):
    with pytest.raises(ValueError, match="days"):
        admin.issue(token_file, "alice", TOOL_NAMES, 91)
    with pytest.raises(ValueError, match="unknown tools"):
        admin.issue(token_file, "alice", frozenset({"bruin_ai"}), 90)


def test_bad_user_id_refused(token_file):
    with pytest.raises(Exception):
        admin.issue(token_file, "../bob", TOOL_NAMES, 90)


def test_world_readable_file_refused(token_file):
    os.chmod(token_file, 0o644)
    with pytest.raises(TokenFileError, match="chmod 600"):
        read_records(token_file)


def test_token_file_broken_at_runtime_refuses_everyone(token_file):
    token = admin.issue(token_file, "alice", TOOL_NAMES, 90)
    store = TokenStore(token_file)
    os.chmod(token_file, 0o644)
    os.utime(token_file, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert store.lookup(token) == (None, "token_file_unavailable")


def test_verifier_and_tool_authorization(token_file):
    token = admin.issue(token_file, "alice", frozenset({"health"}), 90)
    store = TokenStore(token_file)
    access = asyncio.run(TokenStoreVerifier(store, "http://127.0.0.1:18443/mcp").verify_token(token))
    assert access.subject == "alice" and access.token != token  # secret not kept
    assert caller_for_tool(store, access, "health").user_id == "alice"
    with pytest.raises(AuthError, match="not authorized"):
        caller_for_tool(store, access, "inspect_dataset")
    with pytest.raises(AuthError, match="not authenticated"):
        caller_for_tool(store, None, "health")
    admin.revoke(token_file, access.client_id)
    with pytest.raises(AuthError, match="no longer valid"):
        caller_for_tool(store, access, "health")
