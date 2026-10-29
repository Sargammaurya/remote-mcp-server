import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def base_env(tmp_path):
    """A complete, valid dev environment whose paths all live under tmp_path."""
    return {
        "BMCP_ENV": "dev",
        "BMCP_BIND_HOST": "127.0.0.1",
        "BMCP_PORT": "18443",
        "BMCP_WORKSPACE_ROOT": str(tmp_path / "ws"),
        "BMCP_LOG_DIR": str(tmp_path / "logs"),
        "BRUIN_BIN": str(tmp_path / "bruin"),
        "BRUIN_HOME": str(tmp_path / "bruin-home"),
        "BRUIN_EXPECTED_VERSION": "v0.11.780",
        "BRUIN_VERIFIED_FILE": str(tmp_path / "VERIFIED.txt"),
        "BMCP_PUBLIC_URL": "http://127.0.0.1:18443",
        "BMCP_TOKEN_FILE": str(tmp_path / "auth" / "tokens.json"),
        "BMCP_TMP_DIR": str(tmp_path / "tmp"),
    }


PROJECT = Path(__file__).resolve().parent.parent
VENDOR = PROJECT / "vendor" / "bruin" / "v0.11.780"

needs_bruin = pytest.mark.skipif(not (VENDOR / "bruin").exists(),
                                 reason="run scripts/fetch_bruin.sh first")


@pytest.fixture
def real_env(tmp_path, base_env):
    """base_env, but with the real pinned Bruin. BRUIN_HOME is the project's
    var/bruin-home so the already-downloaded DuckDB driver is reused."""
    return {
        **base_env,
        "BRUIN_BIN": str(VENDOR / "bruin"),
        "BRUIN_VERIFIED_FILE": str(VENDOR / "VERIFIED.txt"),
        "BRUIN_HOME": str(PROJECT / "var" / "bruin-home"),
    }
