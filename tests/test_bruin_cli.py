import hashlib
import os

import pytest

from bruin_mcp_service.bruin_cli import BruinPinError, bruin_env, run_bruin, verify_pinned_binary
from bruin_mcp_service.config import load_settings

FAKE_BRUIN = "#!/bin/sh\necho \"args=$*\"\nenv | sort\n"


@pytest.fixture
def settings(tmp_path, base_env):
    binary = tmp_path / "bruin"
    binary.write_text(FAKE_BRUIN)
    binary.chmod(0o750)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    (tmp_path / "VERIFIED.txt").write_text(f"version=v0.11.780\nbinary_sha256={digest}\n")
    return load_settings(base_env)


def test_pinned_binary_runs_with_minimal_env(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("BMCP_TEST_LEAK", "should-not-reach-bruin")
    result = run_bruin(settings, ["query", "-q", "SELECT 1"], cwd=tmp_path, timeout=10)
    assert result.returncode == 0
    assert "args=query -q SELECT 1" in result.stdout
    assert "TELEMETRY_OPTOUT=true" in result.stdout
    assert f"HOME={settings.bruin_home}" in result.stdout
    assert "BMCP_TEST_LEAK" not in result.stdout


def test_tampered_binary_refused_before_running(settings, tmp_path):
    marker = tmp_path / "ran"
    settings.bruin_bin.write_text(f"#!/bin/sh\ntouch {marker}\n")
    with pytest.raises(BruinPinError, match="does not match"):
        run_bruin(settings, ["version"], cwd=tmp_path, timeout=10)
    assert not marker.exists()


def test_version_mismatch_refused(settings):
    settings.bruin_verified_file.write_text("version=v0.11.791\nbinary_sha256=00\n")
    with pytest.raises(BruinPinError, match="expected v0.11.780"):
        verify_pinned_binary(settings)


def test_missing_verified_file_refused(settings):
    os.remove(settings.bruin_verified_file)
    with pytest.raises(BruinPinError, match="cannot read"):
        verify_pinned_binary(settings)


def test_caller_cannot_override_protected_env(settings):
    env = bruin_env(settings, {"HOME": "/root", "TELEMETRY_OPTOUT": "", "PATH": "/evil", "X": "1"})
    assert env["HOME"] == str(settings.bruin_home)
    assert env["TELEMETRY_OPTOUT"] == "true"
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["X"] == "1"
