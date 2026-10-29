"""The server process must refuse to start, before listening, on unsafe setup."""

import os
import socket
import subprocess
import sys

import pytest

from bruin_mcp_service import admin
from conftest import PROJECT, needs_bruin

pytestmark = needs_bruin
PORT = 18443


def _start(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-m", "bruin_mcp_service.server"],
                          cwd=PROJECT / "src", env={"PATH": "/usr/bin:/bin", **env},
                          capture_output=True, text=True, timeout=30)


def _listening() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


@pytest.fixture
def env(real_env, monkeypatch):
    if _listening():
        pytest.fail(f"127.0.0.1:{PORT} already in use")
    monkeypatch.setenv("BMCP_TOKEN_FILE", real_env["BMCP_TOKEN_FILE"])
    admin.main(["init"])
    return dict(real_env)


def test_refuses_group_readable_token_file(env):
    os.chmod(env["BMCP_TOKEN_FILE"], 0o640)
    r = _start(env)
    assert r.returncode != 0 and "chmod 600" in r.stderr
    assert not _listening()


def test_refuses_missing_token_file(env):
    os.remove(env["BMCP_TOKEN_FILE"])
    r = _start(env)
    assert r.returncode != 0 and "No such file" in r.stderr


def test_refuses_unverified_bruin_binary(env, tmp_path):
    (tmp_path / "VERIFIED.txt").write_text("version=v0.11.780\nbinary_sha256=" + "0" * 64 + "\n")
    r = _start({**env, "BRUIN_VERIFIED_FILE": str(tmp_path / "VERIFIED.txt")})
    assert r.returncode != 0 and "BruinPinError" in r.stderr
    assert not _listening()


@pytest.mark.parametrize("host", ["0.0.0.0", "172.16.21.108"])
def test_dev_refuses_network_bind(env, host):
    r = _start({**env, "BMCP_BIND_HOST": host})
    assert r.returncode != 0 and "loopback" in r.stderr


def test_cleans_stale_scratch_and_staging_on_start(env, tmp_path):
    """Leftovers from a crash are removed at startup (the server is then stopped)."""
    stale_q = tmp_path / "tmp" / "q.crashed"
    stale_q.mkdir(parents=True)
    staging = tmp_path / "ws" / "users" / "alice" / "datasets" / ".staging-ds_0123456789abcdef"
    staging.mkdir(parents=True)
    (staging / "data.csv").write_text("x\n")
    os.chmod(staging, 0o500)
    proc = subprocess.Popen([sys.executable, "-I", "-m", "bruin_mcp_service.server"],
                            cwd=PROJECT / "src", env={"PATH": "/usr/bin:/bin", **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if _listening():
                break
            proc.poll()
            assert proc.returncode is None, "server exited"
            subprocess.run(["sleep", "0.1"])
        assert _listening()
        assert not stale_q.exists() and not staging.exists()
    finally:
        proc.terminate()
        proc.wait(timeout=15)
    assert not _listening()
