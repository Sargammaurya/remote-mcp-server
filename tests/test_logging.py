import json

from bruin_mcp_service.logging_setup import REDACTED, configure_logging, redact


def test_redacts_bearer_and_secret_pairs():
    s = redact("Authorization: Bearer abc.def-123 and password=hunter2, token: t0k")
    assert "abc.def-123" not in s and "hunter2" not in s and "t0k" not in s
    assert s.count(REDACTED) == 3


def test_redacts_nested_structures():
    out = redact({"user": "alice", "api_key": "k", "nested": [{"Password": "p"}, "bearer zzz"]})
    assert out["user"] == "alice"
    assert out["api_key"] == REDACTED
    assert out["nested"][0]["Password"] == REDACTED
    assert "zzz" not in out["nested"][1]


def test_log_file_is_json_and_redacted(tmp_path, capsys):
    logger = configure_logging(tmp_path / "logs")
    logger.info("call with %s", "Bearer s3cr3t", extra={"user_id": "alice", "token": "s3cr3t"})
    for h in logger.handlers:
        h.flush()
    line = (tmp_path / "logs" / "service.log").read_text().strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["user_id"] == "alice"
    assert entry["token"] == REDACTED
    assert "s3cr3t" not in line
    # nothing goes to stdout (stdout belongs to the protocol in stdio setups)
    assert capsys.readouterr().out == ""


def test_identifier_keys_are_kept_but_values_still_scanned():
    out = redact({"token_id": "83c8ca2f1944", "user_id": "alice", "token": "x", "access_token": "y",
                  "session_id": "Bearer leaked"})
    assert out["token_id"] == "83c8ca2f1944" and out["user_id"] == "alice"
    assert out["token"] == REDACTED and out["access_token"] == REDACTED
    assert "leaked" not in out["session_id"]
